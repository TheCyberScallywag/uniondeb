"""The deterministic trigger pass: whole-phrase, case-insensitive, Welsh and English (design §5.3 step 1).

A hit makes a record *question*, and the hits and the list version are stored on the tag, so the
result is auditable and repeatable: the same text and the same list version always give the same
hits. Matching is on whole words, so ``tip`` does not fire on ``multiple``, and is insensitive to
case, Unicode form, typographic apostrophes and runs of whitespace.

People who know a list exists disguise words. A second pass therefore reads the text with the two
cheapest disguises undone (digits and symbols standing in for letters, ``c4sh``; and letters spaced
out, ``d e l e t e``) and reports what it finds as the canonical phrase. It is still a fixed
function of the text, not a judgement.
"""

import hashlib
import os
import re
import unicodedata
from functools import cached_property
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

ENV_PATH = "UNIONDEB_TRIGGER_LIST"
_LEET = str.maketrans({"4": "a", "@": "a", "3": "e", "1": "i", "!": "i", "0": "o", "5": "s", "$": "s", "7": "t"})
_LEET_CHARS = set("4@31!05$7")
_SPACED = re.compile(r"(?<!\w)(?:\w[ .\-_]){3,}\w(?!\w)")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "ʼ": "'"})


_WELSH = frozenset(
    [
        "yn",
        "y",
        "yr",
        "mae",
        "i'r",
        "â",
        "ac",
        "wedi",
        "dwi",
        "ti",
        "chi",
        "nos",
        "diolch",
        "paid",
        "dweud",
        "hwn",
        "rhwng",
        "ni",
        "arian",
        "parod",
        "ar",
        "gyfer",
        "bydd",
        "wyt",
        "pan",
        "am",
        "fi",
        "dy",
        "eich",
        "ein",
        "nhw",
        "hefyd",
        "ond",
        "wrth",
        "neb",
        "un",
        "peth",
        "tro",
        "tu",
        "ôl",
        "ganolfan",
        "heno",
        "wedyn",
        "cyn",
        "dydd",
        "gwych",
        "bawb",
    ]
)


def language(text: str | None) -> str:
    """``cy`` or ``en``, from the share of common Welsh function words. Used only for fairness monitoring."""
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", normalise(text or ""))
    return "cy" if words and sum(w in _WELSH for w in words) / len(words) >= 0.3 else "en"


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_APOSTROPHES).casefold()
    return re.sub(r"\s+", " ", text).strip()


def undisguise(text: str) -> str:
    """``normalise``d text with letter-substitutions and spaced-out letters undone."""

    def token(m: re.Match[str]) -> str:
        t = m.group(0)
        mixed = any(c.isalpha() for c in t) and any(c in _LEET_CHARS for c in t)
        return t.translate(_LEET) if mixed else t

    text = re.sub(r"\S+", token, text)
    return _SPACED.sub(lambda m: re.sub(r"[ .\-_]", "", m.group(0)), text)


class TriggerList(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    owner: str
    status: str = "draft"
    phrases: tuple[str, ...] = Field(min_length=1)

    @classmethod
    def load(cls, path: Path | None = None) -> TriggerList:
        """The list at ``path``, or ``$UNIONDEB_TRIGGER_LIST``, or the packaged draft."""
        path = path or (Path(os.environ[ENV_PATH]) if os.environ.get(ENV_PATH) else None)
        source = path or files("uniondeb.agents") / "config" / "triggers.v1.yaml"
        return cls.model_validate(yaml.safe_load(source.read_text("utf-8")))

    @cached_property
    def sha256(self) -> str:
        return hashlib.sha256("\n".join(sorted({normalise(p) for p in self.phrases})).encode()).hexdigest()

    @cached_property
    def _pattern(self) -> re.Pattern[str]:
        phrases = sorted({normalise(p) for p in self.phrases}, key=len, reverse=True)
        body = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in phrases)
        return re.compile(rf"(?<!\w)(?:{body})(?!\w)")

    def scan(self, text: str | None) -> list[str]:
        """Sorted canonical phrases found in ``text``."""
        if not text:
            return []
        plain = normalise(text)
        hits = {m.group(0) for m in self._pattern.finditer(plain)}
        hits |= {m.group(0) for m in self._pattern.finditer(undisguise(plain))}
        return sorted(re.sub(r"\s+", " ", h) for h in hits)
