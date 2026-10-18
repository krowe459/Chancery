"""Deterministic check that each cited authority actually appears in the opinion.

No API calls. For every entry in a summary's authorities_cited, pull out the
parts that can be matched literally and look for them in the opinion text:

  * reporter citations ("802 A.2d 257", "2014 WL 2768782") - the strongest
    signal, and the part most likely to be invented;
  * a case's first party name ("Telxon" from "Telxon Corp. v. Meyerson"),
    compared on letters and digits only, so "In re ML/EQ" matches "ML EQ";
  * statute section numbers next to "§" or "Section" ("§ 145", "§ 17704.08"),
    and rule numbers ("56", "403").

Matching ignores case, punctuation variants and ALL whitespace, because PDF
extraction breaks citations across lines and pages. A miss is a flag for the
reader, not proof of invention: the model may have paraphrased a name the
opinion abbreviates differently.

Each authority gains:
  check:        "verified" | "not_found" | "not_checked"
  check_detail: the part(s) that were not found (only when not_found)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

REPORTER_RE = re.compile(
    r"\b(\d{1,4})\s+"
    r"(A\.\s?[23]d|A\.|F\.\s?(?:[234]d|4th)|F\.\s?Supp\.\s?(?:[23]d)?|U\.S\.|"
    r"S\.\s?Ct\.|WL|Del\.\s?Ch\.|Del\.|So\.\s?[23]d|N\.E\.\s?[23]d|N\.Y\.S\.\s?[23]d|"
    r"P\.\s?[23]d)\s+(\d{1,7})\b"
)
SECTION_RE = re.compile(r"(?:§+|\bSection|\bSec\.)\s*(\d+(?:[.\-]\d+)*)", re.IGNORECASE)
RULE_RE = re.compile(r"(?:\bRule|\bR\.|\bD\.R\.E\.)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

# Words that say nothing about which case this is.
PARTY_STOPWORDS = {
    "in", "re", "the", "of", "and", "a", "an", "matter", "state", "ex", "rel",
    "inc", "corp", "co", "llc", "lp", "llp", "ltd", "company", "corporation",
}


def _normalize(text: str) -> str:
    """Lowercase, unify quotes/dashes/section signs, drop all whitespace."""
    text = unicodedata.normalize("NFKC", text or "")
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"')
                .replace("–", "-").replace("—", "-")
                .replace("­", ""))
    return re.sub(r"\s+", "", text).lower()


def _alnum(text: str) -> str:
    """Letters and digits only, for party names: "ML/EQ" and "ML EQ" match."""
    return re.sub(r"[^0-9a-z]", "", _normalize(text))


def _found(needle: str, haystack: str, loose: bool = False) -> bool:
    """`haystack` is _normalize()d text, or _alnum()ed text when `loose`."""
    n = _alnum(needle) if loose else _normalize(needle)
    return bool(n) and n in haystack


def _first_party(authority: str) -> str | None:
    """First distinctive word of the case name, e.g. 'telxon', 'el paso'."""
    name = re.split(r",|\(|\s\d", authority, maxsplit=1)[0]
    name = re.split(r"\s+v\.?\s+|\s+vs\.?\s+", name, maxsplit=1)[0]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&.\-]*", name)
             if w.strip(".").lower() not in PARTY_STOPWORDS]
    if not words:
        return None
    first = words[0].strip(".")
    return first if len(first) >= 3 else " ".join(w.strip(".") for w in words[:2])


def _needles(auth: dict[str, Any]) -> list[tuple[bool, tuple[str, ...]]]:
    """Fragments that should appear in the opinion for this authority, as
    (loose, spellings): any one spelling counts, and loose needles (party
    names) are compared on letters and digits only."""
    text = str(auth.get("authority", ""))
    kind = auth.get("kind")
    needles = [(False, (" ".join(m.groups()),)) for m in REPORTER_RE.finditer(text)]

    if kind == "case" or (kind is None and re.search(r"\sv\.?\s|\bIn re\b", text)):
        party = _first_party(text)
        if party:
            needles.append((True, (party,)))
    elif kind == "statute":
        # A bare number like "350" would match page numbers and addresses,
        # so a section number only counts next to "§" or "Section".
        for num in SECTION_RE.findall(text):
            needles.append((False, (f"§{num}", f"section {num}", f"sec. {num}")))
    elif kind == "rule":
        needles += [(False, (num,)) for num in RULE_RE.findall(text) or NUMBER_RE.findall(text)[-1:]]
    # "doctrine" and "other" have nothing reliably literal beyond any reporter cite.
    return list(dict.fromkeys(needles))


def check_authority(auth: dict[str, Any], normalized_text: str,
                    alnum_text: str | None = None) -> dict[str, Any]:
    needles = _needles(auth)
    if not needles:
        return {"check": "not_checked"}
    if alnum_text is None:
        alnum_text = re.sub(r"[^0-9a-z]", "", normalized_text)
    missing = [spellings[0] for loose, spellings in needles
               if not any(_found(v, alnum_text if loose else normalized_text, loose)
                          for v in spellings)]
    if missing:
        return {"check": "not_found", "check_detail": "; ".join(missing)}
    return {"check": "verified"}


def check_citations(summary: dict[str, Any], opinion_text: str) -> dict[str, int]:
    """Annotate summary['authorities_cited'] in place; return status counts."""
    haystack = _normalize(opinion_text)
    loose_haystack = re.sub(r"[^0-9a-z]", "", haystack)
    counts = {"verified": 0, "not_found": 0, "not_checked": 0}
    for auth in summary.get("authorities_cited", []):
        auth.pop("check_detail", None)
        auth.update(check_authority(auth, haystack, loose_haystack))
        counts[auth["check"]] += 1
    return counts
