"""A deterministic, scalable roster of people and their devices.

People are **employees** (CyberScallywags UK staff/volunteers, in teams, some on the
insider list for a confidential client project) or **external** contacts (friends,
family, suppliers, clients). Every mobile has an IMEI and a SIM (ICCID/IMSI/MSISDN);
about half of households keep a landline; most employees have a work computer.

All identifiers come from reserved ranges (see ``identifiers.py``). The Ofcom mobile
drama range holds 1,000 numbers, which caps the number of mobiles per directory.
"""

import random
import unicodedata
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal

from uniondeb.core.identifiers import gen_iccid, gen_imei, gen_imsi, gen_ip, gen_landline, gen_mac, gen_msisdn
from uniondeb.core.models import ComputerDevice, Device, DeviceType, LandlineDevice, MobileDevice

FIRST_NAMES: tuple[str, ...] = (
    "Dafydd",
    "Siân",
    "Rhys",
    "Ffion",
    "Gareth",
    "Nia",
    "Owain",
    "Cerys",
    "Iolo",
    "Elin",
    "Huw",
    "Megan",
    "Bethan",
    "Gruffydd",
    "Lowri",
    "Tomos",
    "Catrin",
    "Aled",
    "Gwen",
    "Emyr",
    "Heledd",
    "Ioan",
    "Manon",
    "Llŷr",
    "Mared",
    "Osian",
    "Rhiannon",
    "Steffan",
    "Teleri",
    "Gwion",
    "Carys",
    "Deiniol",
    "Eirlys",
    "Geraint",
    "Hedd",
    "Leusa",
    "Morgan",
    "Nest",
    "Peredur",
    "Seren",
    "Tegid",
    "Alys",
    "Bryn",
    "Dwynwen",
)
SURNAMES: tuple[str, ...] = (
    "Jones",
    "Davies",
    "Williams",
    "Evans",
    "Thomas",
    "Roberts",
    "Hughes",
    "Lewis",
    "Morgan",
    "Griffiths",
    "Owen",
    "Price",
    "Rees",
    "Jenkins",
    "Powell",
    "Pritchard",
    "Llewellyn",
    "Bowen",
    "Vaughan",
    "Parry",
    "Prosser",
    "Gwynne",
    "Meredith",
    "Probert",
    "Bevan",
    "Howells",
    "Lloyd",
    "Cadwaladr",
    "Rowlands",
    "Pugh",
)
TEAMS: dict[str, tuple[str, ...]] = {
    "Partnerships": ("Partnerships manager", "Client consultant"),
    "Finance": ("Treasurer", "Finance officer"),
    "Events": ("Events coordinator", "Venue volunteer"),
    "Mentoring": ("Mentor", "Mentoring lead"),
    "Infrastructure": ("Infrastructure lead", "Platform engineer"),
    "Outreach": ("Schools outreach officer", "Community organiser"),
    "Training": ("CTF trainer", "Workshop lead"),
}
INSIDER_TEAMS = frozenset({"Partnerships", "Finance"})  # may hold price-sensitive client information

MAX_MOBILES = 1000  # Ofcom 07700 900000–900999
BURNER_HEADROOM = 60  # numbers kept free for scenario burners / new SIMs

type Kind = Literal["employee", "external"]


def _slug(name: str) -> str:
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace(" ", ".")


@dataclass(frozen=True, slots=True)
class Person:
    person_id: str
    name: str
    kind: Kind
    team: str | None = None
    role: str | None = None
    insider_list: bool = False


@dataclass
class Directory:
    """``Directory()`` keeps the original 16-person demo; pass sizes for larger runs."""

    seed: int = 1646
    employees: int = 16
    externals: int = 0
    people: list[Person] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)

    def __post_init__(self) -> None:
        if int(self.employees * 1.25) + self.externals > MAX_MOBILES - BURNER_HEADROOM:
            raise ValueError(
                f"{self.employees} employees + {self.externals} externals needs more than the "
                f"{MAX_MOBILES - BURNER_HEADROOM} mobile numbers available in the Ofcom drama range"
            )
        self._rng = random.Random(self.seed)
        self._numbers: set[str] = set()
        self._ips: set[str] = set()
        self._owner_of: dict[str, Person] = {}
        names = self._unique_names(self.employees + self.externals)
        team_names = list(TEAMS)
        for i in range(self.employees):
            team = team_names[i % len(team_names)]
            person = Person(
                f"CS-{i + 1:04d}",
                names[i],
                "employee",
                team,
                self._rng.choice(TEAMS[team]),
                insider_list=team in INSIDER_TEAMS,
            )
            self._add_person(
                person,
                work_computer=self._rng.random() < 0.8,
                second_mobile=self._rng.random() < 0.2,
                landline=i % 2 == 0,
            )
        for j in range(self.externals):
            person = Person(f"EXT-{j + 1:04d}", names[self.employees + j], "external")
            self._add_person(
                person, work_computer=self._rng.random() < 0.2, second_mobile=False, landline=self._rng.random() < 0.3
            )

    # ── construction ────────────────────────────────────────────────────────────────
    def _unique_names(self, n: int) -> list[str]:
        if n <= 16 and self.externals == 0:  # the original demo cast
            return list(FIRST_NAMES[:n])
        combos = [f"{f} {s}" for f in FIRST_NAMES for s in SURNAMES]
        self._rng.shuffle(combos)
        if n > len(combos):
            raise ValueError(f"at most {len(combos)} unique names")
        return combos[:n]

    def new_mobile(self, owner: str) -> MobileDevice:
        """A fresh handset + SIM (also used for scenario burners)."""
        dev = MobileDevice(
            owner=owner,
            imei=gen_imei(self._rng),
            iccid=gen_iccid(self._rng),
            imsi=gen_imsi(self._rng),
            msisdn=gen_msisdn(self._rng, self._numbers),
        )
        self.devices.append(dev)
        self._invalidate()
        return dev

    def _add_person(self, person: Person, *, work_computer: bool, second_mobile: bool, landline: bool) -> None:
        self.people.append(person)
        rng = self._rng
        new = [
            MobileDevice(
                owner=person.name,
                imei=gen_imei(rng),
                iccid=gen_iccid(rng),
                imsi=gen_imsi(rng),
                msisdn=gen_msisdn(rng, self._numbers),
            )
        ]
        if second_mobile:
            new.append(
                MobileDevice(
                    owner=person.name,
                    imei=gen_imei(rng),
                    iccid=gen_iccid(rng),
                    imsi=gen_imsi(rng),
                    msisdn=gen_msisdn(rng, self._numbers),
                )
            )
        if landline:
            new.append(LandlineDevice(owner=person.name, number=gen_landline(rng, self._numbers)))
        if work_computer:
            new.append(
                ComputerDevice(
                    owner=person.name,
                    ip=gen_ip(rng, v6=rng.random() < 0.25, used=self._ips),
                    mac=gen_mac(rng),
                    hostname=f"{_slug(person.name)}-laptop",
                )
            )
        self.devices.extend(new)
        self._owner_of[person.name] = person

    def _invalidate(self) -> None:
        for attr in ("by_type", "by_owner"):
            self.__dict__.pop(attr, None)

    # ── lookups ─────────────────────────────────────────────────────────────────────
    @cached_property
    def by_type(self) -> dict[DeviceType, list[Device]]:
        out: dict[DeviceType, list[Device]] = {t: [] for t in DeviceType}
        for d in self.devices:
            out[DeviceType(d.type)].append(d)
        return out

    @cached_property
    def by_owner(self) -> dict[str, list[Device]]:
        out: dict[str, list[Device]] = {}
        for d in self.devices:
            out.setdefault(d.owner, []).append(d)
        return out

    def person(self, name: str) -> Person | None:
        return self._owner_of.get(name)

    @property
    def employee_list(self) -> list[Person]:
        return [p for p in self.people if p.kind == "employee"]

    @property
    def external_list(self) -> list[Person]:
        return [p for p in self.people if p.kind == "external"]

    def device_of(self, name: str, kind: DeviceType, index: int = 0) -> Device | None:
        devs = [d for d in self.by_owner.get(name, []) if d.type == kind]
        return devs[index] if len(devs) > index else None

    def pick(self, rng: random.Random, *types: DeviceType, exclude: Device | None = None) -> Device:
        pool = [d for t in (types or tuple(DeviceType)) for d in self.by_type[t] if d != exclude]
        return rng.choice(pool)

    def other_device_of(self, rng: random.Random, device: Device, *types: DeviceType) -> Device | None:
        pool = [d for d in self.by_owner[device.owner] if d != device and (not types or d.type in types)]
        return rng.choice(pool) if pool else None

    def roster_rows(self) -> list[dict[str, object]]:
        return [
            {
                "person_id": p.person_id,
                "name": p.name,
                "kind": p.kind,
                "team": p.team or "",
                "role": p.role or "",
                "insider_list": p.insider_list,
                "device_ids": ";".join(d.id for d in self.by_owner.get(p.name, [])),
            }
            for p in self.people
        ]
