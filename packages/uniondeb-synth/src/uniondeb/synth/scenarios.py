"""Scenario engines: normal traffic, an insider-dealing ring and a drug-supply line.

Every event is emitted together with its ground truth (``Labelled``): the expected
content tag, the code-of-ethics clauses, a typology, and the subject person ids. The
scenarios also return *hidden links* that a good investigation should recover but that
no single record states, such as which employee controls a burner phone.

Traffic always involves at least one CyberScallywags employee, because that is the
only traffic an organisation could legitimately hold.

Implements: UND-012
"""

import math
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from uniondeb.core.mail import COMPANY_DOMAIN, Attachment, EmailAddress, EmailEvent, Journal, JournalSource
from uniondeb.core.models import (
    CallEvent,
    CallStatus,
    Device,
    DeviceType,
    HandoffStep,
    MessageChannel,
    MessageEvent,
    Network,
)

from . import content as C
from .directory import Directory, Person, _slug
from .journeys import JOURNEYS

PLACES = ("Y Caban", "the Pier café", "the hackspace", "the Torch", "the library", "Neuadd y Dref", "the marina")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
COMPANY, TICKER, PROJECT = "Tonnau Ynni plc", "TNNU", "Prosiect Morlo"  # all fictional

# relative activity by hour of day (weekday); weekends are scaled down
HOURS_WEEKDAY = [
    0.2,
    0.1,
    0.05,
    0.05,
    0.05,
    0.1,
    0.4,
    1.2,
    3.0,
    4.0,
    4.0,
    3.5,
    3.0,
    3.5,
    4.0,
    3.8,
    3.2,
    2.5,
    1.8,
    1.5,
    1.2,
    0.9,
    0.6,
    0.3,
]


@dataclass(frozen=True, slots=True)
class Labelled:
    event: CallEvent | MessageEvent | HandoffStep | EmailEvent
    dataset: str  # calls | messages | handoff | email
    scenario: str  # normal | insider_dealing | drug_trafficking
    tag: C.Tag
    clauses: tuple[str, ...] = ()
    typology: str = "none"
    subjects: tuple[str, ...] = ()
    note: str = ""

    @property
    def key(self) -> str:
        e = self.event
        match e:
            case CallEvent():
                return e.call_id
            case MessageEvent():
                return e.msg_id
            case EmailEvent():
                return e.message_id
            case _:
                return f"{e.conversation_id}#{e.seq}"

    @property
    def at(self) -> datetime:
        e = self.event
        if isinstance(e, CallEvent):
            return e.started_at
        return e.sent_at if isinstance(e, (MessageEvent, EmailEvent)) else e.at


@dataclass
class ScenarioResult:
    name: str
    records: list[Labelled] = field(default_factory=list)
    actors: dict[str, Any] = field(default_factory=dict)
    hidden_links: list[dict[str, str]] = field(default_factory=list)
    timeline: list[dict[str, str]] = field(default_factory=list)


# ───────────────────────────── event builders (always schema-valid) ──────────────────
def _network(a: Device, b: Device) -> Network:
    if DeviceType.COMPUTER in (a.type, b.type):
        return Network.VOIP
    return Network.VOLTE if a.type == b.type == DeviceType.MOBILE else Network.PSTN


def call(
    a: Device,
    b: Device,
    at: datetime,
    *,
    status: CallStatus = CallStatus.ANSWERED,
    duration: int = 60,
    transcript: str | None = None,
) -> CallEvent:
    return CallEvent(
        call_id=f"call-{uuid.uuid7()}",
        caller=a,
        callee=b,
        started_at=at,
        duration_s=0 if status == CallStatus.MISSED else duration,
        status=status,
        network=_network(a, b),
        transcript=transcript,
    )


def message(
    rng: random.Random,
    s: Device,
    r: Device,
    at: datetime,
    body: str,
    *,
    delivered: bool | None = None,
    channel: MessageChannel | None = None,
) -> MessageEvent:
    if channel is None:
        if r.type == DeviceType.LANDLINE:
            channel = MessageChannel.TEXT_TO_LANDLINE
        elif s.type == DeviceType.COMPUTER:
            channel = MessageChannel.WEB
        else:
            channel = rng.choices((MessageChannel.SMS, MessageChannel.RCS, MessageChannel.MMS), (0.6, 0.32, 0.08))[0]
    if delivered is None:
        delivered = rng.random() < (0.85 if channel == MessageChannel.TEXT_TO_LANDLINE else 0.97)
    return MessageEvent(
        msg_id=f"msg-{uuid.uuid7()}",
        sender=s,
        recipient=r,
        channel=channel,
        body_chars=len(body),
        sent_at=at,
        delivered=delivered,
        body=body,
    )


def _at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour % 24, minute, second), tzinfo=UTC) + timedelta(days=hour // 24)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:  # normal approximation
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def _ctx(rng: random.Random, d: Directory) -> dict[str, str]:
    return {
        "place": rng.choice(PLACES),
        "time": f"{rng.randint(5, 8)}pm",
        "day": rng.choice(DAYS),
        "first": rng.choice(d.employee_list).name.split()[0],
        "company": COMPANY,
        "ticker": TICKER,
        "project": PROJECT,
        "n": str(rng.randint(1000, 9999)),
    }


def _devices(d: Directory, p: Person, *types: DeviceType) -> list[Device]:
    return [x for x in d.by_owner.get(p.name, []) if not types or x.type in types]


# ───────────────────────────── email helpers ────────────────────────────────────────
EXTERNAL_DOMAINS = ("example.org", "example.net")


def email_address(p: Person, personal: bool = False) -> EmailAddress:
    first, _, last = _slug(p.name).partition(".")
    if personal:
        return EmailAddress(address=f"{first}{last}@mail.example", name=p.name)
    if p.kind == "employee":
        return EmailAddress(address=f"{first}.{last}@{COMPANY_DOMAIN}", name=p.name)
    domain = EXTERNAL_DOMAINS[sum(map(ord, p.person_id)) % len(EXTERNAL_DOMAINS)]
    return EmailAddress(address=f"{first}.{last}@{domain}", name=p.name)


def work_device(d: Directory, p: Person) -> Device | None:
    return (_devices(d, p, DeviceType.COMPUTER) or _devices(d, p, DeviceType.MOBILE) or [None])[0]


def sha(rng: random.Random) -> str:
    return f"{rng.getrandbits(256):064x}"


def email(
    rng: random.Random,
    d: Directory,
    sender: Person,
    to: list[EmailAddress],
    at: datetime,
    t: C.EmailTemplate,
    ctx: dict[str, str],
    *,
    cc: list[EmailAddress] | None = None,
    bcc: list[EmailAddress] | None = None,
    source: JournalSource = JournalSource.M365_ENVELOPE,
    attachment_sha: str | None = None,
    in_reply_to: str | None = None,
) -> EmailEvent:
    cc, bcc = cc or [], bcc or []
    attachments = []
    if t.attachment:
        attachments.append(
            Attachment(
                filename=t.attachment,
                content_type=_mime(t.attachment),
                size=rng.randint(12_000, 2_400_000),
                sha256=attachment_sha or sha(rng),
            )
        )
    envelope = [*to, *cc, *bcc] if source == JournalSource.M365_ENVELOPE else []
    if source != JournalSource.M365_ENVELOPE:
        bcc = []  # BCC-style journaling can't see the other BCC recipients
    return EmailEvent(
        message_id=f"<{uuid.uuid7()}@{COMPANY_DOMAIN}>",
        sender=email_address(sender),
        to=to,
        cc=cc,
        bcc=bcc,
        subject=t.subject.format(**ctx),
        body=t.body.format(**ctx),
        sent_at=at,
        in_reply_to=in_reply_to,
        attachments=attachments,
        device=work_device(d, sender),
        journal=Journal(
            source=source, received_at=at + timedelta(seconds=rng.randint(1, 6)), envelope_recipients=envelope
        ),
    )


def _mime(filename: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(filename.rsplit(".", 1)[-1], "application/octet-stream")


# ───────────────────────────── normal traffic ───────────────────────────────────────
@dataclass
class NormalConfig:
    msgs_per_employee_day: float = 10.0
    calls_per_employee_day: float = 4.0
    handoffs_per_employee_day: float = 0.12
    recorded_call_share: float = 0.35  # share of answered calls with a transcript/notes
    benign_trigger_rate: float = 0.03  # share of messages using a trigger word innocently
    weekend_factor: float = 0.25
    emails_per_employee_day: float = 5.0
    journal_source: JournalSource = JournalSource.M365_ENVELOPE


def normal_day(rng: random.Random, d: Directory, day: date, cfg: NormalConfig) -> Iterator[Labelled]:
    factor = 1.0 if day.weekday() < 5 else cfg.weekend_factor
    employees, externals = d.employee_list, d.external_list
    by_team: dict[str | None, list[Person]] = {}
    for e in employees:
        by_team.setdefault(e.team, []).append(e)

    def when() -> datetime:
        h = rng.choices(range(24), HOURS_WEEKDAY)[0]
        return _at(day, h, rng.randrange(60), rng.randrange(60))

    def counterparty(e: Person) -> Person:
        if externals and rng.random() < 0.4:
            return rng.choice(externals)
        pool = by_team[e.team] if rng.random() < 0.6 and len(by_team[e.team]) > 1 else employees
        while (c := rng.choice(pool)) is e:
            pass
        return c

    for e in employees:
        # messages
        for _ in range(_poisson(rng, cfg.msgs_per_employee_day * factor)):
            other = counterparty(e)
            outgoing = rng.random() < 0.6
            a, b = (e, other) if outgoing else (other, e)
            senders = _devices(d, a, DeviceType.MOBILE, DeviceType.COMPUTER)
            receivers = _devices(d, b)
            if not senders or not receivers:
                continue
            s, r = rng.choice(senders), rng.choice(receivers)
            benign = rng.random() < cfg.benign_trigger_rate
            t = rng.choice(C.BENIGN_TRIGGER_MESSAGES if benign else C.NORMAL_MESSAGES)
            yield Labelled(
                message(rng, s, r, when(), C.render(t, **_ctx(rng, d))),
                "messages",
                "normal",
                t.tag,
                t.clauses,
                "benign_trigger" if benign else "none",
                (e.person_id,),
            )
        # calls
        for _ in range(_poisson(rng, cfg.calls_per_employee_day * factor)):
            other = counterparty(e)
            a, b = (e, other) if rng.random() < 0.55 else (other, e)
            ca, cb = _devices(d, a), _devices(d, b)
            if not ca or not cb:
                continue
            x, y = rng.choice(ca), rng.choice(cb)
            status = rng.choices(list(CallStatus), (0.72, 0.12, 0.07, 0.09))[0]
            duration = {
                CallStatus.ANSWERED: max(5, min(5400, int(rng.lognormvariate(4.4, 0.9)))),
                CallStatus.DROPPED: rng.randint(3, 240),
                CallStatus.VOICEMAIL: rng.randint(5, 90),
            }.get(status, 0)
            t = None
            if status == CallStatus.ANSWERED and rng.random() < cfg.recorded_call_share:
                benign = rng.random() < cfg.benign_trigger_rate * 2
                t = rng.choice(C.BENIGN_TRIGGER_TRANSCRIPTS if benign else C.NORMAL_TRANSCRIPTS)
            ev = call(
                x, y, when(), status=status, duration=duration, transcript=C.render(t, **_ctx(rng, d)) if t else None
            )
            yield Labelled(
                ev,
                "calls",
                "normal",
                t.tag if t else "clean",
                t.clauses if t else (),
                "benign_trigger" if t and t.tag == "question" else "none",
                (e.person_id,),
            )
        # email (journaled: only mail *sent* from company accounts is captured)
        for _ in range(_poisson(rng, cfg.emails_per_employee_day * factor)):
            to = [counterparty(e) for _ in range(rng.choices((1, 2, 3), (0.7, 0.2, 0.1))[0])]
            cc = [counterparty(e)] if rng.random() < 0.25 else []
            bcc = [counterparty(e)] if rng.random() < 0.04 else []
            benign = rng.random() < cfg.benign_trigger_rate
            t = rng.choice(C.BENIGN_TRIGGER_EMAILS if benign else C.NORMAL_EMAILS)
            ev = email(
                rng,
                d,
                e,
                [email_address(p) for p in to],
                when(),
                t,
                _ctx(rng, d),
                cc=[email_address(p) for p in cc],
                bcc=[email_address(p) for p in bcc],
                source=cfg.journal_source,
            )
            yield Labelled(
                ev, "email", "normal", t.tag, t.clauses, "benign_trigger" if benign else "none", (e.person_id,)
            )
        # cross-device hand-offs
        for _ in range(_poisson(rng, cfg.handoffs_per_employee_day * factor)):
            have = {x.type: x for x in _devices(d, e)}
            options = [(n, j) for n, j in JOURNEYS.items() if {s[0] for s in j} <= set(have)]
            if not options:
                continue
            name, journey = rng.choice(options)
            conv = f"conv-{name}-{uuid.uuid7()}"
            at = when()
            for seq, (dtype, ch, action) in enumerate(journey, start=1):
                at += timedelta(seconds=rng.randint(20, 1800))
                note = C.render(rng.choice(C.NORMAL_NOTES)) if rng.random() < 0.3 else None
                ev = HandoffStep(
                    conversation_id=conv, seq=seq, device=have[dtype], channel=ch, action=action, at=at, content=note
                )
                yield Labelled(ev, "handoff", "normal", "clean", (), "none", (e.person_id,))


# ───────────────────────────── insider dealing ──────────────────────────────────────
def insider_dealing(rng: random.Random, d: Directory, start: date, days: int) -> ScenarioResult:
    """A Partnerships employee on the insider list tips a friend before a (fictional) takeover
    announcement. The friend trades through a broker and tips a relative. The pattern: channel
    switching to landline, a contact spike before the announcement, concealment requests."""
    res = ScenarioResult("insider_dealing")
    insiders = [p for p in d.employee_list if p.insider_list and p.team == "Partnerships"]
    insider = next((p for p in insiders if _devices(d, p, DeviceType.LANDLINE)), insiders[0])
    finance = next(p for p in d.employee_list if p.team == "Finance")
    ext = [p for p in d.external_list if _devices(d, p, DeviceType.MOBILE)]
    if len(ext) < 3:
        raise ValueError("insider_dealing needs at least 3 external contacts (use --externals)")
    tippee = next((p for p in ext if _devices(d, p, DeviceType.LANDLINE)), ext[0])
    relative, broker = [p for p in ext if p is not tippee][:2]

    mob = lambda p: _devices(d, p, DeviceType.MOBILE)[0]  # noqa: E731
    pc = lambda p: (_devices(d, p, DeviceType.COMPUTER) or _devices(d, p, DeviceType.MOBILE))[0]  # noqa: E731
    land = lambda p: (_devices(d, p, DeviceType.LANDLINE) or _devices(d, p, DeviceType.MOBILE))[0]  # noqa: E731

    ann = start + timedelta(days=int(days * 0.6))
    ann += timedelta(days=(7 - ann.weekday()) % 7)  # next Monday
    if ann >= start + timedelta(days=days):
        ann -= timedelta(days=7)
    D = lambda off, h, m=0: _at(ann + timedelta(days=off), h, m, rng.randrange(60))  # noqa: E731
    ctx = {"company": COMPANY, "ticker": TICKER, "project": PROJECT}
    subj = (insider.person_id,)
    I = C.INSIDER  # noqa: E741

    def msg(key, s, r, at, who=subj):
        t = I[key]
        res.records.append(
            Labelled(
                message(rng, s, r, at, C.render(t, **ctx), delivered=True),
                "messages",
                res.name,
                t.tag,
                t.clauses,
                t.typology,
                who,
                key,
            )
        )

    def ring(key, a, b, at, dur, who=subj):
        t = I[key]
        res.records.append(
            Labelled(
                call(a, b, at, duration=dur, transcript=C.render(t, **ctx)),
                "calls",
                res.name,
                t.tag,
                t.clauses,
                t.typology,
                who,
                key,
            )
        )

    msg("board_pack", pc(insider), pc(finance), D(-14, 10, 5))
    msg("compliance_reminder", pc(finance), pc(insider), D(-12, 9, 30), (finance.person_id,))
    ring("tip_call", mob(insider), mob(tippee), D(-9, 19, 40), 372)
    msg("how_sure", mob(tippee), mob(insider), D(-9, 19, 52))
    msg("switch_channel", mob(insider), mob(tippee), D(-9, 19, 55))
    ring("landline_detail", land(insider), land(tippee), D(-9, 21, 10), 541)
    ring("broker_order", mob(tippee), mob(broker), D(-8, 8, 15), 188, (insider.person_id, tippee.person_id))
    msg("family_tip", mob(tippee), mob(relative), D(-8, 8, 40), (insider.person_id, tippee.person_id))
    msg("delete_chat", mob(insider), mob(tippee), D(-6, 22, 5))
    msg("still_on", mob(tippee), mob(insider), D(-1, 20, 30))
    msg("rns_time", mob(insider), mob(tippee), D(-1, 20, 31))
    msg("celebrate", mob(tippee), mob(insider), D(1, 10, 15))
    msg("sell_half", mob(tippee), mob(broker), D(1, 10, 20), (tippee.person_id,))
    # email: the board pack goes to the team, then the insider forwards it to a personal account
    E_ = C.INSIDER_EMAILS
    team = [p for p in d.employee_list if p.team == "Partnerships" and p is not insider][:3] or [finance]
    pack_sha = sha(rng)

    def mail(key, sender, to, at, who=subj, **kw):
        t = E_[key]
        res.records.append(
            Labelled(
                email(rng, d, sender, to, at, t, ctx, **kw), "email", res.name, t.tag, t.clauses, t.typology, who, key
            )
        )

    mail(
        "board_pack_email",
        insider,
        [email_address(p) for p in team],
        D(-14, 10, 20),
        cc=[email_address(finance)],
        attachment_sha=pack_sha,
    )
    mail(
        "forward_to_personal", insider, [email_address(insider, personal=True)], D(-14, 21, 37), attachment_sha=pack_sha
    )
    mail("email_tippee", insider, [email_address(tippee)], D(-9, 19, 48))

    # contact spike: untranscribed evening calls in the run-up (pattern, not content)
    for off in range(-10, 0):
        if rng.random() < 0.55:
            a, b = (land(insider), land(tippee)) if rng.random() < 0.5 else (mob(insider), mob(tippee))
            res.records.append(
                Labelled(
                    call(a, b, D(off, rng.randint(19, 22), rng.randrange(60)), duration=rng.randint(40, 400)),
                    "calls",
                    res.name,
                    "clean",
                    (),
                    "insider_contact_spike",
                    subj,
                    "evening call, no transcript",
                )
            )

    res.actors = {
        "insider": {"person_id": insider.person_id, "name": insider.name, "team": insider.team},
        "tippee": {"person_id": tippee.person_id, "name": tippee.name, "relationship": "friend (external)"},
        "relative": {"person_id": relative.person_id, "name": relative.name},
        "broker": {"person_id": broker.person_id, "name": broker.name},
        "control": {"person_id": finance.person_id, "name": finance.name, "note": "legitimate insider-list reminder"},
        "company": COMPANY,
        "ticker": TICKER,
        "project": PROJECT,
        "fictional": True,
    }
    res.timeline = [{"at": D(0, 7).isoformat(), "event": f"{COMPANY} takeover announced via regulatory news"}]
    res.hidden_links = [
        {
            "type": "TIPPED",
            "from": insider.person_id,
            "to": tippee.person_id,
            "evidence": "tip_call, landline_detail, rns_time",
        },
        {"type": "TIPPED", "from": tippee.person_id, "to": relative.person_id, "evidence": "family_tip"},
        {"type": "TRADED_VIA", "from": tippee.person_id, "to": broker.person_id, "evidence": "broker_order"},
        {
            "type": "EXFILTRATED",
            "from": insider.person_id,
            "to": email_address(insider, personal=True).address,
            "evidence": f"board pack attachment sha256 {pack_sha[:12]}… forwarded to a personal webmail account",
        },
    ]
    return res


# ───────────────────────────── drug supply line ─────────────────────────────────────
def drug_trafficking(
    rng: random.Random, d: Directory, start: date, days: int, exclude: set[str] | None = None
) -> ScenarioResult:
    """An Events employee runs a supply line from an unregistered PAYG phone, rotates to a
    new number mid-period, checks in with the line from their own mobile, and once uses
    their work laptop and the hackspace for a drop. Customers are external contacts."""
    res = ScenarioResult("drug_trafficking")
    exclude = exclude or set()
    employee = next(
        p
        for p in d.employee_list
        if p.team == "Events" and _devices(d, p, DeviceType.COMPUTER) and p.person_id not in exclude
    )
    pool = [p for p in d.external_list if p.person_id not in exclude and _devices(d, p, DeviceType.MOBILE)]
    if len(pool) < 8:
        raise ValueError("drug_trafficking needs at least 8 external contacts (use --externals)")
    supplier, customers = pool[0], pool[1 : 1 + min(24, len(pool) - 1)]
    burner1 = d.new_mobile("Unregistered PAYG")
    burner2 = d.new_mobile("Unregistered PAYG")
    rotate_on = start + timedelta(days=days // 2)
    mob = lambda p: _devices(d, p, DeviceType.MOBILE)[0]  # noqa: E731
    subj = (employee.person_id,)
    X = C.DRUGS

    def line(day: date):
        return burner1 if day < rotate_on else burner2

    def msg(key, s, r, at, note=""):
        t = X[key]
        res.records.append(
            Labelled(
                message(rng, s, r, at, C.render(t)),
                "messages",
                res.name,
                t.tag,
                t.clauses,
                t.typology,
                subj,
                note or key,
            )
        )

    for i in range(days):
        day = start + timedelta(days=i)
        busy = day.weekday() in (3, 4, 5)  # Thu–Sat
        L = line(day)
        for c in rng.sample(customers, k=min(len(customers), rng.randint(4, 9) if busy else rng.randint(0, 3))):
            at = _at(day, rng.randint(18, 25), rng.randrange(60))
            key = rng.choices(("customer_ask", "customer_order", "customer_coded"), (0.5, 0.3, 0.2))[0]
            msg(key, mob(c), L, at)
            if rng.random() < 0.6:  # short call follows
                res.records.append(
                    Labelled(
                        call(mob(c), L, at + timedelta(minutes=rng.randint(2, 20)), duration=rng.randint(12, 60)),
                        "calls",
                        res.name,
                        "clean",
                        (),
                        "supply_short_call",
                        subj,
                        "short call, no transcript",
                    )
                )
        if busy and rng.random() < 0.7:
            for c in rng.sample(customers, k=min(6, len(customers))):
                msg(rng.choice(("line_broadcast", "line_cash")), L, mob(c), _at(day, 18, rng.randrange(30)))
        if day.weekday() == 2:  # weekly restock
            msg("supplier_reup", mob(supplier), L, _at(day, 14, rng.randrange(60)))
        if day.weekday() in (1, 4):  # handler check-in from the employee's own phone
            transcript = C.render(X["handler_call"]) if rng.random() < 0.3 else None
            t = X["handler_call"] if transcript else None
            res.records.append(
                Labelled(
                    call(
                        mob(employee),
                        L,
                        _at(day, 23, rng.randrange(50)),
                        duration=rng.randint(30, 95),
                        transcript=transcript,
                    ),
                    "calls",
                    res.name,
                    t.tag if t else "clean",
                    t.clauses if t else (),
                    "supply_handler_checkin",
                    subj,
                    "employee mobile → line",
                )
            )
        if day == rotate_on:
            for c in customers:
                msg("line_new_number", burner2, mob(c), _at(day, 17, rng.randrange(60)))
        if i == days // 3:  # one drop arranged from the work laptop, involving the hackspace
            msg("work_laptop_drop", _devices(d, employee, DeviceType.COMPUTER)[0], L, _at(day, 16, 42))
        if i == days // 3 + 1:  # the work account arranges a drop at the hackspace
            t = C.DRUG_EMAILS["locker_email"]
            res.records.append(
                Labelled(
                    email(rng, d, employee, [email_address(customers[0])], _at(day, 17, 5), t, {}),
                    "email",
                    res.name,
                    t.tag,
                    t.clauses,
                    t.typology,
                    subj,
                    "locker_email",
                )
            )
        if day.weekday() == 4 and rng.random() < 0.5:
            t = C.DRUG_EMAILS["event_cover"]
            colleague = next(p for p in d.employee_list if p is not employee)
            res.records.append(
                Labelled(
                    email(rng, d, employee, [email_address(colleague)], _at(day, 12, 30), t, {}),
                    "email",
                    res.name,
                    t.tag,
                    t.clauses,
                    t.typology,
                    subj,
                    "event_cover",
                )
            )
        if rng.random() < 0.15:
            msg("benign_late", mob(rng.choice(customers)), L, _at(day, 26, rng.randrange(60)))

    res.actors = {
        "employee": {"person_id": employee.person_id, "name": employee.name, "team": employee.team},
        "lines": [burner1.id, burner2.id],
        "rotation_date": rotate_on.isoformat(),
        "supplier": supplier.person_id,
        "customers": [c.person_id for c in customers],
    }
    res.hidden_links = [
        {
            "type": "CONTROLS",
            "from": employee.person_id,
            "to": burner1.id,
            "evidence": "handler check-in calls from the employee's mobile; work-laptop message",
        },
        {"type": "CONTROLS", "from": employee.person_id, "to": burner2.id, "evidence": "same pattern after rotation"},
        {
            "type": "SAME_LINE",
            "from": burner1.id,
            "to": burner2.id,
            "evidence": "new-number broadcast to same customers",
        },
    ]
    return res
