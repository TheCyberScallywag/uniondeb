#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Create (or update) the Uniondeb backlog on GitHub from backlog/backlog.yaml.

Implements: UND-010

Idempotent: issues are matched by their "[UND-NNN]" / "[EPIC-N]" title prefix, so re-running
only fills gaps. Uses your own `gh` login, which matters for user-owned Projects:

    gh auth refresh -s project            # once: the project scope is needed for Projects v2
    uv run scripts/bootstrap_backlog.py --dry-run
    uv run scripts/bootstrap_backlog.py --create-fields

What it does:
  1. labels (phase:*, type:*, priority:*, size:*, owner:human) and one milestone per epic
  2. epic issues, then step issues with acceptance criteria as checklists
  3. each step attached to its epic as a GitHub sub-issue; dependencies linked in the body
  4. every issue added to Project 6, with Status / Phase / Priority / Size set where those
     fields exist (--create-fields creates Phase, Priority and Size if missing)
  5. backlog/issue-map.json written, so commits, PRs and TRACEABILITY.md can resolve IDs
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BACKLOG = ROOT / "backlog" / "backlog.yaml"
MAP = ROOT / "backlog" / "issue-map.json"
PLAN_URL = "https://github.com/{repo}/blob/main/docs/uniondeb-plan.md"

LABEL_COLOURS = {"phase": "1f6feb", "type": "8250df", "priority": "d1242f", "size": "bf8700", "owner": "57606a"}
PRIORITY_DESC = {"P0": "must have for the phase", "P1": "should have", "P2": "could have"}

DRY = False


def gh(*args: str, json_out: bool = False, check: bool = True) -> str | dict | list:
    cmd = ["gh", *args]
    if DRY and _is_write(args):  # reads still run, so a dry run shows real gaps
        print("DRY:", " ".join(a if len(a) < 80 else a[:77] + "..." for a in cmd))
        return {} if json_out else ""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise SystemExit(f"gh failed: {' '.join(cmd)}\n{res.stderr.strip()}")
    return json.loads(res.stdout or "null") if json_out else res.stdout.strip()


def _is_write(args: tuple[str, ...]) -> bool:
    joined = " ".join(args)
    return args[:2] in (("label", "create"), ("issue", "create"), ("issue", "edit")) or any(
        w in joined for w in ("item-add", "item-edit", "field-create", "-X POST", "-X PATCH")
    )


def phase_of(epic_id: str) -> str:
    return epic_id.split("-", 1)[1].lower()  # EPIC-2B → 2b


def labels_for(step: dict) -> list[str]:
    out = [
        f"phase:{phase_of(step['epic'])}",
        f"type:{step.get('type', 'feature')}",
        f"priority:{step.get('priority', 'P1')}",
        f"size:{step.get('size', 'M')}",
    ]
    if step.get("owner") == "human":
        out.append("owner:human")
    return out


def ensure_labels(repo: str, backlog: dict) -> None:
    wanted: dict[str, str] = {"epic": "0e8a16"}
    for s in backlog["steps"]:
        for lab in labels_for(s):
            wanted[lab] = LABEL_COLOURS[lab.split(":")[0]]
    for e in backlog["epics"]:
        wanted[f"phase:{phase_of(e['id'])}"] = LABEL_COLOURS["phase"]
    for name, colour in wanted.items():
        desc = PRIORITY_DESC.get(name.split(":")[-1], "") if name.startswith("priority:") else ""
        gh("label", "create", name, "--repo", repo, "--color", colour, "--description", desc, "--force")


def ensure_milestones(repo: str, backlog: dict) -> dict[str, int]:
    existing = gh("api", f"repos/{repo}/milestones?state=all&per_page=100", json_out=True) or []
    by_title = {m["title"]: m["number"] for m in existing}
    for e in backlog["epics"]:
        if e["milestone"] not in by_title:
            m = gh(
                "api",
                f"repos/{repo}/milestones",
                "-X",
                "POST",
                "-f",
                f"title={e['milestone']}",
                "-f",
                f"description={e['title']} — plan {e['plan']}",
                json_out=True,
            )
            by_title[e["milestone"]] = (m or {}).get("number", 0)
    return by_title


def existing_issues(repo: str) -> dict[str, dict]:
    issues = (
        gh(
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "number,title,url,id",
            json_out=True,
        )
        or []
    )
    out = {}
    for i in issues:
        m = re.match(r"^\[(UND-\d{3}|EPIC-\w+)\]", i["title"])
        if m:
            out[m.group(1)] = i
    return out


def epic_body(e: dict, repo: str, children: list[dict]) -> str:
    rows = "\n".join(f"- [ ] **{s['id']}** {s['title']}" for s in children)
    return (
        f"**Epic {e['id']}** · plan {e['plan']} · [docs/uniondeb-plan.md]({PLAN_URL.format(repo=repo)})\n\n"
        f"### Steps\n{rows}\n\n_Steps are attached as sub-issues. Generated from `backlog/backlog.yaml`._\n"
    )


def step_body(s: dict, repo: str, numbers: dict[str, int]) -> str:
    deps = s.get("depends_on", [])
    dep_txt = ", ".join(f"#{numbers[d]} ({d})" if d in numbers else d for d in deps) or "none"
    acc = "\n".join(f"- [ ] {a}" for a in s.get("acceptance", []))
    slug = re.sub(r"[^a-z0-9]+", "-", s["title"].lower()).strip("-")[:40].strip("-")
    who = "**People task** (governance), not code." if s.get("owner") == "human" else ""
    return f"""**{s["id"]}** · epic {s["epic"]} · priority {s.get("priority")} · size {s.get("size")} · plan {s.get("plan", "")}
{who}

{s.get("summary", "").strip()}

### Acceptance criteria
{acc}

### Depends on
{dep_txt}

### Traceability
- Branch: `{s["id"].lower()}-{slug}`
- Commits carry the trailer `Refs: {s["id"]}`; the PR body says `Closes #<this issue>`
- Code implementing this step says `Implements: {s["id"]}` in its module docstring
- Matrix: [`TRACEABILITY.md`](https://github.com/{repo}/blob/main/TRACEABILITY.md)

_Generated from `backlog/backlog.yaml`; edit there and re-run `scripts/bootstrap_backlog.py`._
"""


def create_issue(repo: str, title: str, body: str, labels: list[str], milestone: str | None) -> dict:
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    for lab in labels:
        args += ["--label", lab]
    if milestone:
        args += ["--milestone", milestone]
    url = gh(*args)
    if DRY:
        return {"number": 0, "url": "", "id": ""}
    number = int(str(url).rstrip("/").rsplit("/", 1)[1])
    return {"number": number, "url": url}


# ───────────────────────────── project (Projects v2) ────────────────────────────────
class Project:
    def __init__(self, owner: str, number: int, create_fields: bool) -> None:
        self.owner, self.number = owner, number
        view = gh("project", "view", str(number), "--owner", owner, "--format", "json", json_out=True) or {}
        self.id = view.get("id", "")
        self.fields = self._fields()
        if create_fields:
            for name, options in (
                ("Phase", ["0", "1", "2", "2b", "3", "4", "5", "6", "7", "8", "9"]),
                ("Priority", ["P0", "P1", "P2"]),
                ("Size", ["S", "M", "L"]),
            ):
                if name.lower() not in self.fields:
                    gh(
                        "project",
                        "field-create",
                        str(number),
                        "--owner",
                        owner,
                        "--name",
                        name,
                        "--data-type",
                        "SINGLE_SELECT",
                        "--single-select-options",
                        ",".join(options),
                    )
            self.fields = self._fields()

    def _fields(self) -> dict[str, dict]:
        data = (
            gh("project", "field-list", str(self.number), "--owner", self.owner, "--format", "json", json_out=True)
            or {}
        )
        return {f["name"].lower(): f for f in data.get("fields", [])}

    def add(self, url: str) -> str:
        item = (
            gh(
                "project",
                "item-add",
                str(self.number),
                "--owner",
                self.owner,
                "--url",
                url,
                "--format",
                "json",
                json_out=True,
            )
            or {}
        )
        return item.get("id", "")

    def set(self, item_id: str, field: str, value: str) -> None:
        f = self.fields.get(field.lower())
        if not f or not item_id:
            return
        opt = next((o for o in f.get("options", []) if o["name"].lower() == value.lower()), None)
        if opt:
            gh(
                "project",
                "item-edit",
                "--id",
                item_id,
                "--project-id",
                self.id,
                "--field-id",
                f["id"],
                "--single-select-option-id",
                opt["id"],
            )


def main() -> None:
    global DRY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--create-fields", action="store_true", help="create Phase/Priority/Size project fields if missing")
    ap.add_argument("--no-project", action="store_true", help="issues only (e.g. if the project token is missing)")
    a = ap.parse_args()
    DRY = a.dry_run
    backlog = yaml.safe_load(BACKLOG.read_text())
    repo = backlog["project"]["repo"]

    ensure_labels(repo, backlog)
    ensure_milestones(repo, backlog)
    have = existing_issues(repo)
    issue_map: dict[str, dict] = json.loads(MAP.read_text()) if MAP.exists() else {}
    numbers = {k: v["number"] for k, v in issue_map.items()} | {k: v["number"] for k, v in have.items()}

    epics = {e["id"]: e for e in backlog["epics"]}
    children = {eid: [s for s in backlog["steps"] if s["epic"] == eid] for eid in epics}

    for eid, e in epics.items():
        if eid not in have:
            r = create_issue(
                repo,
                f"[{eid}] {e['title']}",
                epic_body(e, repo, children[eid]),
                ["epic", f"phase:{phase_of(eid)}"],
                e["milestone"],
            )
            have[eid] = r
            numbers[eid] = r["number"]
            print(f"created {eid} → #{r['number']}")
    created: set[str] = set()
    for s in backlog["steps"]:
        if s["id"] not in have:
            created.add(s["id"])
            r = create_issue(
                repo,
                f"[{s['id']}] {s['title']}",
                step_body(s, repo, numbers),
                labels_for(s),
                epics[s["epic"]]["milestone"],
            )
            have[s["id"]] = r
            numbers[s["id"]] = r["number"]
            print(f"created {s['id']} → #{r['number']}")

    # second pass: rewrite new issues' bodies so every dependency is a #link
    for s in backlog["steps"]:
        if s["id"] in created and s.get("depends_on") and numbers.get(s["id"]):
            gh("issue", "edit", str(numbers[s["id"]]), "--repo", repo, "--body", step_body(s, repo, numbers))

    # sub-issues: step → epic
    for s in backlog["steps"]:
        parent, child = numbers.get(s["epic"]), numbers.get(s["id"])
        if parent and child and not issue_map.get(s["id"], {}).get("sub_issue"):
            child_db_id = gh("api", f"repos/{repo}/issues/{child}", "--jq", ".id")
            gh(
                "api",
                f"repos/{repo}/issues/{parent}/sub_issues",
                "-X",
                "POST",
                "-F",
                f"sub_issue_id={child_db_id}",
                check=False,
            )
            issue_map.setdefault(s["id"], {})["sub_issue"] = True

    # project
    if not a.no_project:
        proj = Project(backlog["project"]["owner"], backlog["project"]["number"], a.create_fields)
        for key in [*epics, *(s["id"] for s in backlog["steps"])]:
            entry = issue_map.setdefault(key, {})
            if entry.get("project_item"):
                continue
            url = have.get(key, {}).get("url")
            if not url:
                continue
            item = proj.add(url)
            entry["project_item"] = item
            step = next((s for s in backlog["steps"] if s["id"] == key), None)
            proj.set(item, "Status", "Todo")
            proj.set(item, "Phase", phase_of(step["epic"] if step else key))
            if step:
                proj.set(item, "Priority", step.get("priority", "P1"))
                proj.set(item, "Size", step.get("size", "M"))

    for key, num in numbers.items():
        if num:
            issue_map.setdefault(key, {}).update({"number": num, "url": f"https://github.com/{repo}/issues/{num}"})
    if not DRY:
        MAP.write_text(json.dumps(dict(sorted(issue_map.items())), indent=2) + "\n")
        print(f"wrote {MAP.relative_to(ROOT)} ({len(issue_map)} entries). Now run: uv run scripts/traceability.py")


if __name__ == "__main__":
    sys.exit(main())
