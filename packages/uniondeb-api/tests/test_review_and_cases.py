"""Review to investigation, end to end: four-eyes, pseudonymity, case access, audit, mock SAR."""

import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from uniondeb.api import mint
from uniondeb.cases import SarGlossary
from uniondeb.worker import Worker

from .conftest import SECRET, make, token

A, B, LEAD, NO, INV, INV2 = (token(u) for u in ("ann", "bob", "lead", "officer", "inv", "inv2"))


def _scenarios(run):
    return {s["name"]: s for s in json.loads((run / "ground_truth" / "scenarios.json").read_text())}


async def test_dirty_review_through_the_api_needs_two_reviewers_and_shows_no_identity(tagged_api):
    s, client = tagged_api
    listing = (await client.get("/review/items?kind=dirty_tag&limit=5", headers=A)).json()
    assert listing and all(
        set(i) == {"id", "kind", "state", "four_eyes", "summary", "created_at", "decisions"} for i in listing
    )
    detail = (await client.get(f"/review/items/{listing[0]['id']}", headers=A)).json()
    rec = detail["record"]
    assert rec["text"] and rec["tags"] and all(set(p) == {"role", "device", "party"} for p in rec["parties"])
    names = [r["n"] for r in await s.runner.run("MATCH (p:Person) RETURN p.name AS n")]
    ids = [r["v"] for r in await s.runner.run("MATCH (i:Identifier) RETURN i.value AS v")]
    blob = json.dumps(
        {k: v for k, v in detail.items() if k != "record"} | {"parties": rec["parties"], "tags": rec["tags"]}
    )
    assert not any(x in blob for x in (*names, *ids))

    url = f"/review/items/{detail['id']}/decisions"
    body = {"decision": "confirm_dirty", "rationale": "describes supply"}
    assert (await client.post(url, json={**body, "rationale": " "}, headers=A)).status_code == 422
    assert (await client.post(url, json=body, headers=A)).json()["state"] == "awaiting_second"
    assert (await client.post(url, json=body, headers=A)).status_code == 422
    assert (await client.post(url, json=body, headers=token("compliance"))).status_code == 403
    done = (await client.post(url, json=body, headers=B)).json()
    assert done["state"] == "decided" and done["decided_by"] == ["ann", "bob"]
    after = (await client.get(f"/review/items/{detail['id']}", headers=A)).json()
    assert [t["label"] for t in after["record"]["tags"] if t["current"]] == ["dirty"]
    viewed = await s.audit.events(actor="ann")
    assert any(e.action == "review.view_item" and e.target == f"ReviewItem:{detail['id']}" for e in viewed)
    queues = (await client.get("/review/queues", headers=A)).json()
    assert queues["notification"].endswith("waiting") and not any(
        c.isalpha() and c.isupper() for c in queues["notification"]
    )

    evals = await client.get("/dashboards/evals", headers=token("compliance"))
    report = evals.json()
    assert report["dirty_agreement"] == 1.0 and report["dirty_reviews"] == {"confirm_dirty": 1}
    assert {"cy", "en"} <= set(report["concern_by_language"]) and "Partnerships" in report["concern_by_team"]
    assert (await client.get("/dashboards/evals", headers=A)).status_code == 403


def _holwr_model(subject_query):
    def respond(messages, info):
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "run_cypher"
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("run_cypher", {"query": subject_query})])
        rows = json.loads(str(returns[-1].content).split(" (first")[0])
        if "glossary_codes" in json.dumps(info.output_tools[0].parameters_json_schema):
            args = {
                "glossary_codes": [],
                "reason_for_suspicion": f"{len(rows)} flagged records in the window.",
                "record_keys": [r["k"] for r in rows],
            }
        else:
            args = {"answer": f"{len(rows)} flagged records.", "record_keys": [r["k"] for r in rows]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond, model_name="scripted")


@pytest.mark.parametrize(("scenario", "actor"), [("insider_dealing", "insider"), ("drug_trafficking", "employee")])
async def test_scenario_is_investigated_end_to_end_to_a_mock_sar(tagged_run, tagged_store, tmp_path, scenario, actor):
    subject = _scenarios(tagged_run[0])[scenario]["actors"][actor]
    calendar = [
        {"date": t["at"][:10], "label": "client announcement"}
        for t in _scenarios(tagged_run[0])["insider_dealing"]["timeline"]
    ]
    s, client = make(
        tagged_store,
        tmp_path,
        holwr=_holwr_model("MATCH (r:Record) WHERE r.text IS NOT NULL RETURN r.key AS k LIMIT 5"),
    )
    from uniondeb.agents.gwyliwr import SensitiveDate

    s.calendar = [SensitiveDate.model_validate(c) for c in calendar]
    async with client:
        # 1. a scan, queued by compliance, raises pseudonymous alerts
        scan = {"kind": "on_demand", "window_to": "2026-09-16T00:00:00+00:00", "window_days": 16}
        assert (await client.post("/scans", json=scan, headers=A)).status_code == 403
        job = (await client.post("/scans", json=scan, headers=token("compliance"))).json()["job"]
        await s.runner.run(
            "MATCH (j:Job {id: $id}) SET j.args_json = $a", {"id": job, "a": json.dumps({**scan, "baseline_days": 0})}
        )
        await Worker(s).drain()
        dashboard = (await client.get("/dashboards/concern", headers=token("compliance"))).json()
        assert dashboard["latest"]["people"] and all(
            p["subject"].startswith("S-") for p in dashboard["latest"]["people"]
        )
        assert subject["name"] not in json.dumps(dashboard) and dashboard["alerts"]["open"] > 0
        assert (await client.get("/dashboards/concern", headers=A)).status_code == 403

        # 2. reviewers see the alert without a name; only a compliance lead can resolve it, with a reason, logged
        [row] = await s.runner.run(
            "MATCH (a:Alert)-[:ABOUT]->(:Person {person_id: $p}) RETURN a.id AS id", {"p": subject["person_id"]}
        )
        alert_id = row["id"]
        items = (await client.get("/review/items?kind=alert&limit=200", headers=A)).json()
        full = [(await client.get(f"/review/items/{i['id']}", headers=A)).json() for i in items]
        item = next(i for i in full if i["payload"]["alert_id"] == alert_id)
        assert subject["name"] not in json.dumps(full) and item["alert"]["subject_pseudonym"].startswith("S-")
        assert (
            await client.post(f"/review/alerts/{alert_id}/identity", json={"reason": "x"}, headers=A)
        ).status_code == 403
        assert (
            await client.post(f"/review/alerts/{alert_id}/identity", json={"reason": " "}, headers=LEAD)
        ).status_code == 422
        who = await client.post(
            f"/review/alerts/{alert_id}/identity", json={"reason": "conflict check before review"}, headers=LEAD
        )
        assert who.json()["person_id"] == subject["person_id"]

        # 3. an evidence pack, with no recommendation; reviewers rate it
        pack = (await client.post(f"/review/alerts/{alert_id}/pack", headers=A)).json()
        assert pack["timeline"] and pack["recommendation"] is None and pack["alternative_explanations"]
        assert (await client.get(f"/review/packs/{pack['id']}", headers=B)).json()["id"] == pack["id"]
        assert (
            await client.post(f"/review/packs/{pack['id']}/rating", json={"rating": 4}, headers=A)
        ).status_code == 204

        # 4. escalation to a case takes two reviewers
        url = f"/review/items/{item['id']}/decisions"
        body = {
            "decision": "open_case",
            "rationale": "pattern warrants investigation",
            "case_pack_version": str(pack["version"]),
        }
        assert (
            await client.post(url, json={"decision": "escalate_legal", "rationale": "x"}, headers=A)
        ).status_code == 422
        assert (await client.post(url, json=body, headers=A)).json()["state"] == "awaiting_second"
        assert await s.runner.run("MATCH (c:Case) RETURN c.id AS id") == []
        assert (await client.post(url, json=body, headers=B)).json()["state"] == "decided"
        [c] = await s.runner.run(
            "MATCH (c:Case)-[:SUBJECT]->(p:Person) RETURN c.id AS id, p.person_id AS pid, c.origin AS origin"
        )
        case_id = c["id"]
        assert (c["pid"], c["origin"]) == (subject["person_id"], "alert")

        # 5. nobody but assigned people can see the case, and its existence does not leak
        base = f"/cases/{case_id}"
        assert (await client.get("/cases", headers=INV)).json() == []
        for path in (
            base,
            f"{base}/timeline",
            f"{base}/contacts",
            f"{base}/notes",
            f"{base}/flags",
            "/cases/nope/timeline",
        ):
            assert (await client.get(path, headers=INV)).status_code == 404, path
        assert (await client.get(base, headers=A)).status_code == 403 and (
            await client.get(base, headers=token("auditor"))
        ).status_code == 403
        assert (
            await client.post("/cases", json={"person": subject["person_id"], "reason": "x"}, headers=INV)
        ).status_code == 403
        assert (
            await client.post("/cases", json={"person": subject["person_id"], "reason": " "}, headers=NO)
        ).status_code == 422

        # 6. assignment is the nominated officer's, with a conflict check
        [mate] = await s.runner.run(
            "MATCH (p:Person {person_id: $p}), (m:Person {team: p.team}) WHERE m <> p RETURN m.person_id AS pid LIMIT 1",
            {"p": subject["person_id"]},
        )
        [other] = await s.runner.run(
            "MATCH (p:Person {person_id: $p}), (m:Person {kind: 'employee'}) WHERE m.team <> p.team AND NOT EXISTS { (m)-[:USES]->(:Device {kind: 'landline'})<-[:USES]-(p) } "
            "RETURN m.person_id AS pid LIMIT 1",
            {"p": subject["person_id"]},
        )
        assign = f"{base}/assignments"
        assert (
            await client.post(assign, json={"investigator_id": "inv", "person_id": other["pid"]}, headers=INV)
        ).status_code == 403
        conflict = await client.post(assign, json={"investigator_id": "inv", "person_id": mate["pid"]}, headers=NO)
        assert conflict.status_code == 409 and "same_team" in conflict.json()["rules"]
        assert (await client.post(assign, json={"investigator_id": "inv"}, headers=NO)).status_code == 409
        assert (
            await client.post(assign, json={"investigator_id": "inv", "person_id": other["pid"]}, headers=NO)
        ).status_code == 200
        assert (await client.get(base, headers=INV2)).status_code == 404

        # 7. the workspace is pseudonymous until the investigator asks, with a reason
        case = (await client.get(base, headers=INV)).json()
        assert subject["name"] not in json.dumps(case) and case["investigators"] == ["inv"]
        timeline = (await client.get(f"{base}/timeline", headers=INV)).json()
        flagged = [r for r in timeline if r["tag"] in ("question", "dirty") or r["proposals"]]
        assert (
            flagged and all(r["text"] for r in flagged) and all(r["text"] is None for r in timeline if r not in flagged)
        )
        contacts = (await client.get(f"{base}/contacts", headers=INV)).json()
        assert contacts and (scenario != "drug_trafficking" or any(c["unregistered"] for c in contacts))
        assert (await client.post(f"{base}/identity", json={"reason": ""}, headers=INV)).status_code == 422
        profile = (
            await client.post(f"{base}/identity", json={"reason": "need to interview the subject"}, headers=INV)
        ).json()
        assert profile["identity"]["name"] == subject["name"] and profile["devices"]

        # 8. provenance, tags, flags, notes
        key = flagged[0]["key"]
        prov = (await client.get(f"{base}/records/{key}/provenance", headers=INV)).json()
        assert len(prov["source_sha256"]) == 1 and prov["steps"][0]["kind"] == "Ingest" and prov["tags"]
        assert (await client.get(f"{base}/records/messages:not-in-case/provenance", headers=INV)).status_code == 404
        tag = {"label": "dirty", "rationale": "confirmed on interview", "clauses": ["CE1"]}
        assert (
            await client.post(f"{base}/records/{key}/tags", json={**tag, "rationale": " "}, headers=INV)
        ).status_code == 422
        assert (await client.post(f"{base}/records/{key}/tags", json=tag, headers=INV)).status_code == 201
        assert (
            next(r for r in (await client.get(f"{base}/timeline", headers=INV)).json() if r["key"] == key)["method"]
            == "human"
        )
        assert (
            await client.post(f"{base}/flags", json={"label": "made_up", "reason": "x"}, headers=INV)
        ).status_code in (409, 422)
        assert (
            await client.post(
                f"{base}/flags", json={"label": "legal_hold", "reason": "litigation expected"}, headers=INV
            )
        ).status_code == 201
        assert key in await s.flags.records_on_hold()
        person_flag = {
            "label": "under_investigation",
            "reason": "case open",
            "target_label": "Person",
            "target_key": f"owner:{subject['name']}",
        }
        assert (await client.post(f"{base}/flags", json=person_flag, headers=INV)).status_code == 201
        assert {f["label"] for f in (await client.get(f"{base}/flags", headers=INV)).json()} == {
            "legal_hold",
            "under_investigation",
        }
        from uniondeb.core.prov import EntityRef

        assert (
            await s.flags.public_flags(EntityRef(label="Person", key=f"owner:{subject['name']}")) == []
        )  # restricted: invisible outside
        assert (await client.post(f"{base}/notes", json={"text": "Interview booked."}, headers=INV)).status_code == 201

        # 9. Holwr answers from the case extract, cites its records, shows its Cypher, and is logged
        answer = (await client.post(f"{base}/holwr", json={"question": "What was flagged?"}, headers=INV)).json()
        assert answer["record_keys"] and answer["cypher"] == [
            "MATCH (r:Record) WHERE r.text IS NOT NULL RETURN r.key AS k LIMIT 5"
        ]
        [known] = await s.runner.run(
            "UNWIND $keys AS k MATCH (r:Record {key: k}) RETURN count(r) AS n", {"keys": answer["record_keys"]}
        )
        assert known["n"] == len(answer["record_keys"])
        narrative = (await client.post(f"{base}/sar/narrative", headers=INV)).json()
        assert narrative["reason_for_suspicion"] and not narrative["refused_actions"]

        # 10. the SAR: drafted by the investigator, approved by a second person, submitted to the mock
        code = next(iter(SarGlossary.load().codes))
        fields = {
            "subject": {
                "name": subject["name"],
                "date_of_birth": "UNKNOWN",
                "address": "",
                "identifiers": [subject["person_id"]],
                "occupation": "UNKNOWN",
            },
            "glossary_codes": [code],
            "reason_for_suspicion": f"{code} {narrative['reason_for_suspicion']}",
            "record_keys": answer["record_keys"],
        }
        draft = (await client.post(f"{base}/sar/drafts", json=fields, headers=INV)).json()
        approval = f"/sar/drafts/{draft['id']}/approval"
        assert (await client.post(approval, headers=INV)).status_code == 403
        invalid = await client.post(approval, headers=NO)
        assert invalid.status_code == 422 and invalid.json()["errors"]
        fields["subject"]["address"] = "UNKNOWN"
        assert (await client.put(f"/sar/drafts/{draft['id']}", json=fields, headers=INV)).status_code == 200
        sub = (await client.post(approval, headers=NO)).json()
        assert sub["reference"].startswith("MOCK-SAR-") and sub["mock"] and len(sub["payload_sha256"]) == 64
        assert (sub["drafted_by"], sub["approved_by"]) == ("inv", "officer")
        assert (await client.post(approval, headers=NO)).status_code == 409
        assert (await client.get(f"{base}/sar/submissions", headers=INV)).status_code == 403
        assert [x["reference"] for x in (await client.get(f"{base}/sar/submissions", headers=NO)).json()] == [
            sub["reference"]
        ]
        assert "sar_submitted" in {f["label"] for f in (await client.get(f"{base}/flags", headers=INV)).json()}

        # 11. closing needs an outcome and a retention review
        assert (
            await client.post(f"{base}/closure", json={"outcome": "sar_submitted", "retention_review": ""}, headers=NO)
        ).status_code == 422
        assert (
            await client.post(
                f"{base}/closure",
                json={"outcome": "sar_submitted", "retention_review": "Hold content six years."},
                headers=NO,
            )
        ).json()["state"] == "closed"

        # 12. everything above is in a chain that verifies; only auditors read it, and they see no case content
        assert (await client.get("/audit/events", headers=INV)).status_code == 403
        events = (await client.get(f"/audit/events?case_id={case_id}&limit=1000", headers=token("auditor"))).json()
        actions = {e["action"] for e in events}
        screens = {
            "portal.timeline",
            "portal.contacts",
            "portal.provenance",
            "portal.tag_record",
            "portal.holwr",
            "portal.sar_narrative",
        }
        assert screens <= actions and {"case.reveal_identity", "holwr.question", "case.access_denied"} <= actions
        reveal = next(e for e in events if e["action"] == "case.reveal_identity")
        assert reveal["actor"] == "inv" and reveal["reason"] == "need to interview the subject"
        assert any(e["action"] == "case.access_denied" and e["actor"] == "inv2" for e in events)
        asked = next(e for e in events if e["action"] == "holwr.question")
        assert json.loads(asked["detail_json"])["cypher"] == answer["cypher"]
        assert any(
            e["action"] == "alert.reveal_identity"
            for e in (await client.get("/audit/events?actor=lead", headers=token("auditor"))).json()
        )
        verify = (await client.get("/audit/verification", headers=token("auditor"))).json()
        assert verify["ok"] and verify["checked"] >= len(events)
        await s.runner.run("MATCH (e:AuditEvent {action: 'case.reveal_identity'}) SET e.reason = 'routine'")
        assert (await client.get("/audit/verification", headers=token("auditor"))).json()["ok"] is False


async def test_every_case_read_endpoint_writes_an_audit_event(tagged_run, tagged_api):
    s, client = tagged_api
    subject = _scenarios(tagged_run[0])["insider_dealing"]["actors"]["insider"]
    case = (
        await client.post(
            "/cases", json={"person": subject["person_id"], "reason": "manual: whistleblower report"}, headers=NO
        )
    ).json()
    base = f"/cases/{case['id']}"
    for path in (
        "/cases",
        base,
        f"{base}/timeline",
        f"{base}/contacts",
        f"{base}/notes",
        f"{base}/flags",
        f"{base}/sar/drafts",
        f"{base}/sar/submissions",
    ):
        before = (await s.audit.head()).seq
        assert (await client.get(path, headers=NO)).status_code == 200, path
        assert (await s.audit.head()).seq > before, f"{path} left no audit event"
    assert (
        await client.post(f"{base}/holwr", json={"question": "anything?"}, headers=NO)
    ).status_code == 503  # no model configured


async def test_pages_render_and_forms_work_for_each_role(tagged_run, tagged_api):
    s, client = tagged_api
    subject = _scenarios(tagged_run[0])["insider_dealing"]["actors"]["insider"]
    assert (await client.get("/console")).status_code == 303 and (await client.get("/login")).status_code == 200

    def cookie(user, roles):
        return {"Cookie": "uniondeb_token=" + mint(SECRET, user, roles), "Origin": "http://uniondeb.test"}

    ann, bob, officer = (
        cookie("ann", ["reviewer"]),
        cookie("bob", ["reviewer"]),
        cookie("officer", ["nominated_officer"]),
    )
    login = await client.post(
        "/login", data={"token": mint(SECRET, "ann", ["reviewer"])}, headers={"Origin": "http://uniondeb.test"}
    )
    assert (
        login.status_code == 303
        and "httponly" in login.headers["set-cookie"].lower()
        and "samesite=strict" in login.headers["set-cookie"].lower()
    )
    assert (await client.post("/login", data={"token": "bad"})).headers["location"].startswith("/login?error=")

    console = await client.get("/console?kind=dirty_tag", headers=ann)
    assert console.status_code == 200 and "dirty_tag" in console.text and "no-store" in console.headers["cache-control"]
    assert (
        console.headers["x-frame-options"] == "DENY"
        and "default-src 'none'" in console.headers["content-security-policy"]
    )
    assert (await client.get("/console", headers=officer)).status_code == 403
    [first] = (await client.get("/review/items?kind=dirty_tag&limit=1", headers=A)).json()
    page = await client.get(f"/console/items/{first['id']}", headers=ann)
    assert page.status_code == 200 and "needs two reviewers" in page.text and "confirm_dirty" in page.text
    post = f"/console/items/{first['id']}"
    assert (
        (await client.post(post, data={"decision": "confirm_dirty", "rationale": ""}, headers=ann))
        .headers["location"]
        .startswith(post + "?error=")
    )
    assert (
        await client.post(
            post, data={"decision": "confirm_dirty", "rationale": "yes"}, headers={"Cookie": ann["Cookie"]}
        )
    ).status_code == 403
    assert (await client.post(post, data={"decision": "confirm_dirty", "rationale": "yes"}, headers=ann)).headers[
        "location"
    ] == "/console"
    assert (await client.post(post, data={"decision": "confirm_dirty", "rationale": "agree"}, headers=bob)).headers[
        "location"
    ] == "/console"
    assert "current" in (await client.get(post, headers=ann)).text

    await s.runner.run(
        "MATCH (r:Record {key: $k}) SET r.body = '<script>alert(1)</script>'",
        {"k": (await client.get(f"/review/items/{first['id']}", headers=A)).json()["record"]["key"]},
    )
    escaped = (await client.get(post, headers=ann)).text
    assert "<script>alert(1)</script>" not in escaped and "&lt;script&gt;" in escaped

    assert "No cases are assigned to you" in (await client.get("/portal", headers=cookie("inv", ["investigator"]))).text
    opened = await client.post(
        "/portal", data={"person": subject["person_id"], "reason": "manual open"}, headers=officer
    )
    url = opened.headers["location"]
    workspace = await client.get(url, headers=officer)
    assert (
        workspace.status_code == 200 and subject["name"] not in workspace.text and "Reveal identity" in workspace.text
    )
    assert (await client.get(url, headers=cookie("inv", ["investigator"]))).status_code == 404
    revealed = await client.post(f"{url}/reveal", data={"reason": "interview"}, headers=officer)
    assert subject["name"] in revealed.text and "has been logged" in revealed.text
    assert (
        (await client.post(f"{url}/reveal", data={"reason": ""}, headers=officer))
        .headers["location"]
        .startswith(url + "?error=")
    )
    assert (await client.post(f"{url}/note", data={"text": "first note"}, headers=officer)).status_code == 303
    assert (
        await client.post(
            f"{url}/flag", data={"label": "legal_hold", "reason": "hold", "target_label": ""}, headers=officer
        )
    ).status_code == 303
    after = (await client.get(url, headers=officer)).text
    assert "first note" in after and "legal_hold" in after
    assert (
        (await client.post(f"{url}/holwr", data={"question": "who?"}, headers=officer))
        .headers["location"]
        .startswith(url + "?error=")
    )
    assert (await client.post(f"{url}/nonsense", data={}, headers=officer)).status_code == 404
