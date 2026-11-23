"""Telecom device identifiers: validated types and safe synthetic generators.

How a device is identified depends on its type:

  mobile    IMEI (handset) + SIM: ICCID (card), IMSI (subscriber), MSISDN (phone number)
  landline  E.164 number (no SIM, no IMEI)
  computer  IP address (+ optional MAC, SIP URI)

Generated values come from ranges that can't collide with real subscribers:
  * MSISDN    Ofcom drama range 07700 900000–900999            → +447700900xxx
  * landline  Ofcom drama ranges 029 2018 0xxx (Cardiff) and 01632 960xxx (no area;
              Ofcom's suggestion for areas without their own range, e.g. 01646)
  * IMSI      MCC 001 / MNC 01, the ITU-T test network
  * ICCID     89 + 001 test country code; Luhn check digit
  * IMEI      TAC prefix 00 (test/reserved); Luhn check digit
  * IP        RFC 5737 (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), RFC 3849 (2001:db8::/32)
"""

import ipaddress
import random
import re
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field

# ───────────────────────────── check digits ─────────────────────────────────────────


def luhn_check_digit(payload: str) -> str:
    """Luhn check digit for a numeric string (used by IMEI and ICCID)."""
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:  # double every 2nd digit, starting from the right of the payload
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_valid(number: str) -> bool:
    return number.isdigit() and len(number) > 1 and luhn_check_digit(number[:-1]) == number[-1]


# ───────────────────────────── validators ────────────────────────────────────────────


def _imei(v: str) -> str:
    if not (len(v) == 15 and luhn_valid(v)):
        raise ValueError("IMEI must be 15 digits with a valid Luhn check digit")
    return v


def _imsi(v: str) -> str:
    if not (v.isdigit() and len(v) == 15):
        raise ValueError("IMSI must be 15 digits (MCC 3 + MNC 2-3 + MSIN)")
    return v


def _iccid(v: str) -> str:
    if not (v.startswith("89") and len(v) in (19, 20) and luhn_valid(v)):
        raise ValueError("ICCID must start 89, be 19-20 digits and pass Luhn")
    return v


_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _e164(v: str) -> str:
    if not _E164.match(v):
        raise ValueError("number must be E.164, e.g. +447700900123")
    return v


def _uk_mobile(v: str) -> str:
    if not _e164(v).startswith("+447"):
        raise ValueError("MSISDN must be a UK mobile number (+447…)")
    return v


def _uk_geographic(v: str) -> str:
    if _e164(v)[:4] not in ("+441", "+442"):
        raise ValueError("landline must be a UK geographic number (+441… / +442…)")
    return v


_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def _mac(v: str) -> str:
    v = v.lower().replace("-", ":")
    if not _MAC.match(v):
        raise ValueError("MAC must look like 02:00:5e:10:00:01")
    return v


# ── value-format variants, normalised before validation ──
def _digits_only(v: object) -> object:
    """'35-209900-176148-1' / '8944 1234 …' → digits only."""
    return re.sub(r"[\s\-./]", "", v) if isinstance(v, str) else (str(v) if isinstance(v, int) else v)


def _to_e164_uk(v: object) -> object:
    """'07700 900123', '(029) 2018 0123', '0044 7700…', '447700…', 'tel:+44…' → '+44…'."""
    if isinstance(v, int):
        v = str(v)
    if not isinstance(v, str):
        return v
    s = re.sub(r"[\s\-().]", "", v.removeprefix("tel:"))
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0"):
        s = "+44" + s[1:]
    elif s.startswith("44"):
        s = "+" + s
    return s


def _mac_format(v: object) -> object:
    """'02-00-5E-10-00-01', '0200.5e10.0001', '02005e100001' → '02:00:5e:10:00:01'."""
    if not isinstance(v, str):
        return v
    h = re.sub(r"[^0-9a-fA-F]", "", v).lower()
    return ":".join(h[i : i + 2] for i in range(0, 12, 2)) if len(h) == 12 else v


Digits = BeforeValidator(_digits_only)
E164 = BeforeValidator(_to_e164_uk)

IMEI = Annotated[
    str,
    Digits,
    AfterValidator(_imei),
    Field(description="15-digit IMEI (TAC 8 + serial 6 + Luhn)", examples=["001234567890125"]),
]
IMSI = Annotated[
    str,
    Digits,
    AfterValidator(_imsi),
    Field(description="15-digit IMSI (MCC + MNC + MSIN)", examples=["001010000000123"]),
]
ICCID = Annotated[
    str,
    Digits,
    AfterValidator(_iccid),
    Field(description="SIM card serial, 19-20 digits starting 89, Luhn", examples=["8900101000000001234"]),
]
MSISDN = Annotated[
    str, E164, AfterValidator(_uk_mobile), Field(description="Mobile number, E.164", examples=["+447700900123"])
]
LandlineNumber = Annotated[
    str, E164, AfterValidator(_uk_geographic), Field(description="Geographic number, E.164", examples=["+442920180123"])
]
MacAddress = Annotated[
    str,
    BeforeValidator(_mac_format),
    AfterValidator(_mac),
    Field(description="Locally administered MAC", examples=["02:00:5e:10:00:01"]),
]


# ───────────────────────────── safe generators ───────────────────────────────────────
TEST_PLMN = "00101"  # MCC 001 / MNC 01: ITU test network
_DOC_V4 = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
_DOC_V6 = ipaddress.ip_network("2001:db8::/32")


def gen_imei(rng: random.Random) -> str:
    body = "00" + f"{rng.randrange(10**12):012d}"  # TAC '00xxxxxx' + 6-digit serial
    return body + luhn_check_digit(body)


def gen_imsi(rng: random.Random) -> str:
    return TEST_PLMN + f"{rng.randrange(10**10):010d}"


def gen_iccid(rng: random.Random) -> str:
    body = "89" + "001" + "01" + f"{rng.randrange(10**11):011d}"  # MII 89 · CC 001 · issuer 01 · account
    return body + luhn_check_digit(body)


def gen_msisdn(rng: random.Random, used: set[str]) -> str:
    return _unique(lambda: f"+447700900{rng.randrange(1000):03d}", used)


def gen_landline(rng: random.Random, used: set[str]) -> str:
    if rng.random() < 0.5:
        return _unique(lambda: f"+442920180{rng.randrange(1000):03d}", used)  # Cardiff 029 2018 0xxx
    return _unique(lambda: f"+441632960{rng.randrange(1000):03d}", used)  # 01632 960xxx


def gen_ip(rng: random.Random, v6: bool = False, used: set[str] | None = None) -> str:
    """Documentation-range address; falls back to IPv6 once the 762 IPv4 addresses run out."""
    used = used if used is not None else set()

    def v4() -> str:
        return str(rng.choice(_DOC_V4)[rng.randrange(1, 255)])

    def v6_() -> str:
        return str(_DOC_V6[rng.randrange(1, 2**32)])

    if not v6:
        try:
            return _unique(v4, used, attempts=200)
        except RuntimeError:
            pass
    return _unique(v6_, used)


def gen_mac(rng: random.Random) -> str:
    octets = [0x02] + [rng.randrange(256) for _ in range(5)]  # 0x02 = locally administered, unicast
    return ":".join(f"{o:02x}" for o in octets)


def _unique(make, used: set[str], attempts: int = 5000) -> str:
    for _ in range(attempts):
        v = make()
        if v not in used:
            used.add(v)
            return v
    raise RuntimeError("drama number range exhausted (1000 numbers per range)")
