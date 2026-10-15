"""Organization normalizers (design 03 §4).

Each normalizer is pure and versioned. Its dictionaries are part of its
configuration hash, so editing a dictionary yields a new component version and
a feature rebuild, never a silent change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any, NamedTuple

from thisness.core.components import ComponentRef
from thisness.core.observation import Observation
from thisness.profiles.base import Feature

# Legal-form tokens and their canonical spelling. Stripped only from the end of a
# name, so "Co-operative Bank" and "AG Barr" keep their first token.
LEGAL_FORMS = {
    "inc": "inc", "incorporated": "inc", "llc": "llc", "corp": "corp", "corporation": "corp",
    "co": "co", "company": "co", "ltd": "ltd", "limited": "ltd", "plc": "plc", "llp": "llp",
    "lp": "lp", "lllp": "lllp", "pc": "pc", "pllc": "pllc", "ulc": "ulc", "gmbh": "gmbh",
    "ag": "ag", "kg": "kg", "ohg": "ohg", "se": "se", "sa": "sa", "sas": "sas", "sarl": "sarl",
    "srl": "srl", "spa": "spa", "bv": "bv", "nv": "nv", "ab": "ab", "oy": "oy", "oyj": "oyj",
    "as": "as", "asa": "asa", "aps": "aps", "pty": "pty", "pte": "pte", "kk": "kk",
}  # fmt: skip

# Abbreviations and inflections mapped to one canonical token.
TOKEN_CANON = {
    "intl": "international", "international": "international",
    "natl": "national", "national": "national",
    "mfg": "manufacturing", "manufacturing": "manufacturing",
    "tech": "technology", "technology": "technology", "technologies": "technology",
    "svc": "services", "svcs": "services", "service": "services", "services": "services",
    "inds": "industries", "industry": "industries", "industries": "industries",
    "sys": "systems", "systems": "systems",
    "soln": "solutions", "solns": "solutions", "solutions": "solutions",
    "assoc": "associates", "associates": "associates",
    "bros": "brothers", "brothers": "brothers",
    "grp": "group", "group": "group",
    "hldgs": "holdings", "holdings": "holdings",
    "mgmt": "management", "management": "management",
    "dist": "distribution", "distribution": "distribution",
    "eng": "engineering", "engineering": "engineering",
    "labs": "laboratories", "laboratory": "laboratories", "laboratories": "laboratories",
    "ctr": "center", "centre": "center", "center": "center",
    "univ": "university", "university": "university",
    "prods": "products", "products": "products",
}  # fmt: skip

ACRONYM_STOPWORDS = frozenset({"and", "of", "the", "for", "de", "la", "du", "der"})

# OCR and keyboard confusables, folded for frequency lookups only: a consistent
# misreading of a common name ("Prernier" for "Premier") must not look like a rare one.
CONFUSABLES = (("rn", "m"), ("vv", "w"), ("0", "o"), ("1", "l"), ("5", "s"))

# Free mail providers say nothing about which organization a person works for.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com",
        "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
        "protonmail.com", "gmx.com", "gmx.de", "web.de", "mail.com", "yandex.com", "qq.com",
        "163.com", "126.com", "zoho.com",
    }
)  # fmt: skip

# Second-level public suffixes, so "ibm.co.uk" keeps three labels.
TWO_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "com.au", "net.au",
        "org.au", "co.nz", "co.jp", "co.in", "co.za", "com.br", "com.mx", "com.cn",
        "com.sg", "com.hk", "co.kr", "com.tr", "com.ar",
    }
)  # fmt: skip

COUNTRY_NAMES = {
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US",
    "america": "US", "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "gbr": "GB", "germany": "DE",
    "deutschland": "DE", "deu": "DE", "canada": "CA", "can": "CA", "france": "FR",
    "fra": "FR", "mexico": "MX", "mex": "MX", "china": "CN", "chn": "CN", "japan": "JP",
    "jpn": "JP", "india": "IN", "ind": "IN", "australia": "AU", "aus": "AU",
    "netherlands": "NL", "the netherlands": "NL", "nld": "NL", "switzerland": "CH",
    "che": "CH", "ireland": "IE", "irl": "IE", "italy": "IT", "ita": "IT", "spain": "ES",
    "esp": "ES", "brazil": "BR", "brasil": "BR", "bra": "BR",
    # As Companies House writes them (design 07 §13.2).
    "northern ireland": "GB", "england and wales": "GB", "cayman islands": "KY",
    "virgin islands british": "VG", "british virgin islands": "VG", "jersey": "JE",
    "guernsey": "GG", "isle of man": "IM", "bermuda": "BM", "luxembourg": "LU",
    "singapore": "SG", "hong kong": "HK",
}  # fmt: skip


def _fold(text: str) -> str:
    """NFKD, drop combining marks, casefold: 'Nestlé' -> 'nestle'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _values(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [str(v) for v in items if v is not None and str(v).strip()]


def _features(name: str, values: Iterable[str]) -> list[Feature]:
    return [Feature(name, i, v) for i, v in enumerate(sorted(set(values)))]


# --- names ------------------------------------------------------------------------


# --- cleaning (design 08 §3) --------------------------------------------------------------
# What supplier extracts add to a name. Patterns are configuration: changing one is a new
# normalizer version. Numbers inside a name ("Intercede 2296 Ltd", "Studio 54", "Omega
# Holdings II", "No. 3") are never removed; only a trailing number after a separator or a
# reference word, or a bare trailing number of five or more digits.

# A card or wallet descriptor: "SQ *", "TST* ", "PAYPAL *", "SP * ".
PROCESSOR_PREFIX = r"^[A-Za-z]{2,7} ?\* ?(?=\S)"
# Card statements cut descriptors at 22 characters (25 on some networks): a descriptor of
# exactly that length may end in a cut word; a longer or shorter one was not cut.
DESCRIPTOR_LENGTHS = (22, 25)
# Notes an ERP keeps in the name field, and the record status each means.
NOTE_STATUS = {
    "do not use": "do_not_use", "dnu": "do_not_use", "inactive": "inactive",
    "deleted": "inactive", "blocked": "blocked", "old": "old", "obsolete": "old",
    "duplicate": "duplicate", "dup": "duplicate", "use vendor": "use_other",
    "use supplier": "use_other",
}  # fmt: skip
NOTE_PREFIX = r"^(?:(?P<zz>z{2,}|x{2,})[\s\-_.:*]+|\*\s*(?P<word>inactive|do not use|deleted|blocked)\s*\*\s*)"
# Phrases that are a note wherever they end the name, and words that are one only when set
# off by a separator or brackets ("Old Mutual" and "Smith Dup" keep theirs).
NOTE_SUFFIX = (r"[\s,;:*]*[-\u2013(\[]?\s*(?P<note>do not use|dnu|inactive|deleted|blocked"
               r"|duplicate(?:\s+of)?(?:\s*#?\s*\d+)?|use\s+(?:vendor|supplier)\s*#?\s*\d+)"
               r"\s*[)\]*.]*\s*$")  # fmt: skip
NOTE_SUFFIX_GUARDED = (r"\s*(?:[-\u2013:,]\s*|[(\[]\s*)(?P<note>old|obsolete|dup(?:\s+of)?\s*#?\s*\d*)"
                       r"\s*[)\]]?\s*$")  # fmt: skip
REFERENCE_WORDS = ("vendor", "vend", "supplier", "ref", "acct", "account", "a/c", "cust", "id")
REFERENCE_SUFFIX = (r"\s*(?:[-#:]\s*#?\s*\d{3,}|\(\s*(?:%s)\.?\s*[:#]?\s*\d+\s*\)"
                    r"|\b(?:%s)\.?\s*[:#]?\s*\d{3,})\s*$")  # fmt: skip
BARE_NUMBER_SUFFIX = r"\s+\d{5,}\s*$"
# Nothing in a name follows its legal form, so a number after one is a reference whatever
# its length: "Amazon, Inc. 4411".
AFTER_LEGAL_FORM = r"(?P<form>\b[A-Za-z][A-Za-z.]*)\.?,?\s+#?\d+\s*$"
LOCATION_WORDS = ("store", "str", "loc", "location", "unit", "bldg", "building", "branch",
                  "shop", "site", "plant", "dept")  # fmt: skip
LOCATION_SUFFIX = r"[\s\-,]*\b(?:%s)\.?\s*#?\s*\d+[a-z]?\s*$"
TRADE_NAME = r"\s+(?:d\s?/?\s?b\s?/?\s?a|t/a|trading as|a/k/a|aka)\.?\s+"

_PROCESSOR = re.compile(PROCESSOR_PREFIX)
_NOTE_PREFIX = re.compile(NOTE_PREFIX, re.I)
_NOTE_SUFFIX = re.compile(NOTE_SUFFIX, re.I)
_NOTE_SUFFIX_GUARDED = re.compile(NOTE_SUFFIX_GUARDED, re.I)
_REFERENCE_WORDS = "|".join(map(re.escape, REFERENCE_WORDS))
_REFERENCE = re.compile(REFERENCE_SUFFIX % (_REFERENCE_WORDS, _REFERENCE_WORDS), re.I)
_BARE_NUMBER = re.compile(BARE_NUMBER_SUFFIX)
_AFTER_LEGAL_FORM = re.compile(AFTER_LEGAL_FORM)
_LOCATION = re.compile(LOCATION_SUFFIX % "|".join(LOCATION_WORDS), re.I)
_TRADE = re.compile(TRADE_NAME, re.I)
_TRAILING_JUNK = re.compile(r"[\s\-\u2013:,;#*]+$")


class CleanedName(NamedTuple):
    text: str  # the name to tokenize
    trade_name: str | None  # "Acme Holdings LLC dba Acme Tools" -> "Acme Tools"
    status: str | None  # what an ERP note said about the record
    descriptor: bool  # a payment-processor descriptor
    truncated: bool  # cut at a descriptor's length: its last word may be partial


def _status_of(note: str) -> str:
    note = re.sub(r"[\d#]+", "", note.casefold()).replace(" of", "").strip()
    note = re.sub(r"\s+", " ", note)
    return NOTE_STATUS.get(note, NOTE_STATUS.get(note.split(" ")[0], "inactive"))


def clean_name(raw: str) -> CleanedName:
    """What a supplier extract adds to a name, removed (design 08 §3). The raw name is never
    changed; this only shapes the features. An empty result falls back to the raw name."""
    text, status, trade = raw.strip(), None, None
    descriptor = truncated = False
    if m := _PROCESSOR.match(text):
        descriptor = True
        text = text[m.end() :]
        # Cut at the descriptor's length, the last word may be part of a word. It is kept
        # (dropping it made "SQ *LOW PROFILE FASHIO" read "Low Profile", another company)
        # and the name is marked, so it is never taken for a whole name (design 08 §13.3).
        truncated = len(raw.lstrip()) in DESCRIPTOR_LENGTHS and len(text.split()) > 1
    if m := _NOTE_PREFIX.match(text):
        status = "inactive" if m.group("zz") else _status_of(m.group("word"))
        text = text[m.end() :]
    if m := _TRADE.search(text):
        text, trade = text[: m.start()], text[m.end() :].strip() or None
    for _ in range(6):  # suffixes stack: "Ltd - 905952 STORE 4990", "Ltd 334750 (OLD)"
        before = text
        for pattern in (_NOTE_SUFFIX, _NOTE_SUFFIX_GUARDED):
            if m := pattern.search(text):
                status = status or _status_of(m.group("note"))
                text = text[: m.start()]
        for pattern in (_LOCATION, _REFERENCE, _BARE_NUMBER):
            text = pattern.sub("", text)
        if (m := _AFTER_LEGAL_FORM.search(text)) and m.group("form").replace(
            ".", ""
        ).casefold() in LEGAL_FORMS:
            text = text[: m.end("form")]
        text = _TRAILING_JUNK.sub("", text)
        if text == before:
            break
    if trade:
        trade = _TRAILING_JUNK.sub("", _REFERENCE.sub("", _LOCATION.sub("", trade))) or None
    if not text.strip():
        return CleanedName(raw.strip(), None, status, descriptor, False)
    return CleanedName(text.strip(), trade, status, descriptor, truncated)


CLEANING = {
    "processor_prefix": PROCESSOR_PREFIX, "descriptor_lengths": DESCRIPTOR_LENGTHS,
    "note_status": NOTE_STATUS, "note_prefix": NOTE_PREFIX, "note_suffix": NOTE_SUFFIX,
    "note_suffix_guarded": NOTE_SUFFIX_GUARDED, "reference_words": REFERENCE_WORDS,
    "reference_suffix": REFERENCE_SUFFIX, "bare_number_suffix": BARE_NUMBER_SUFFIX,
    "location_words": LOCATION_WORDS, "location_suffix": LOCATION_SUFFIX, "trade_name": TRADE_NAME,
}  # fmt: skip


def name_parts(raw: str) -> tuple[list[str], str | None]:
    """Canonical tokens of an organization name, and its legal form if it had one."""
    text = _fold(raw).replace("&", " and ")
    # Dotted initials collapse: "i.b.m." -> "ibm", "s.a." -> "sa", "j.p." -> "jp".
    text = re.sub(r"\b[a-z](?:\.[a-z])+\b\.?", lambda m: m.group(0).replace(".", ""), text)
    tokens = re.findall(r"[a-z0-9]+", text)
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    stripped: list[str] = []
    while len(tokens) > 1:
        last = tokens[-1]
        if last in LEGAL_FORMS:
            stripped.append(LEGAL_FORMS[last])
            tokens = tokens[:-1]
        elif last == "and" and stripped and stripped[-1] == "co":
            tokens = tokens[:-1]  # "& Co" is part of the legal form
        else:
            break
    legal = " ".join(reversed(stripped)) or None
    return [TOKEN_CANON.get(t, t) for t in tokens], legal


def confusable_key(key: str) -> str:
    for a, b in CONFUSABLES:
        key = key.replace(a, b)
    return key


def trigrams(key: str) -> list[str]:
    if len(key) < 3:
        return [key] if key else []
    return sorted({key[i : i + 3] for i in range(len(key) - 2)})


class OrganizationName:
    features = frozenset({"name.key", "name.tf_key", "name.tokens", "name.acronym",
                          "name.trigram", "legal_form", "name.alias_key", "record_status",
                          "name.truncated"})  # fmt: skip
    # v3: names are cleaned first (design 08 §3); a trade name is an alias key; an ERP
    # note is the record's status, never compared.
    ref = ComponentRef.of(
        "normalizer",
        "organization_name",
        "3",
        {"legal_forms": LEGAL_FORMS, "token_canon": TOKEN_CANON,
         "acronym_stopwords": sorted(ACRONYM_STOPWORDS), "confusables": CONFUSABLES,
         "cleaning": CLEANING},
    )  # fmt: skip

    def normalize(self, observation: Observation) -> list[Feature]:
        raw = (observation.attributes or {}).get("name")
        if not isinstance(raw, str) or not raw.strip():
            return []
        cleaned = clean_name(raw)
        tokens, legal = name_parts(cleaned.text)
        if not tokens:
            return []
        key = "".join(tokens)
        out = [Feature("name.key", 0, key), Feature("name.tf_key", 0, confusable_key(key))]
        out += [Feature("name.tokens", i, t) for i, t in enumerate(tokens)]
        out += _features("name.trigram", trigrams(key))
        significant = [t for t in tokens if t not in ACRONYM_STOPWORDS]
        if len(significant) >= 3:
            out.append(Feature("name.acronym", 0, "".join(t[0] for t in significant)))
        if legal:
            out.append(Feature("legal_form", 0, legal))
        if cleaned.trade_name:
            alias, _ = name_parts(cleaned.trade_name)
            if alias and "".join(alias) != key:
                out.append(Feature("name.alias_key", 0, "".join(alias)))
        if cleaned.status:
            out.append(Feature("record_status", 0, cleaned.status))
        if cleaned.truncated:
            out.append(Feature("name.truncated", 0, "1"))
        return out


# --- registration identifiers ------------------------------------------------------------


def lei_is_valid(value: str) -> bool:
    """ISO 17442: 20 alphanumerics whose ISO 7064 MOD 97-10 remainder is 1."""
    if not re.fullmatch(r"[0-9A-Z]{20}", value):
        return False
    return int("".join(str(int(c, 36)) for c in value)) % 97 == 1


CHECKED_SCHEMES = {"LEI": lei_is_valid}


def registration_id(raw: str) -> str | None:
    """'scheme:value' -> 'SCHEME:VALUE' with separators removed, or None when malformed
    or when a scheme with check digits fails them (design 03 §6.2 rule 4)."""
    if ":" not in raw:
        return None
    scheme, value = raw.split(":", 1)
    scheme = re.sub(r"[^A-Za-z0-9]", "", scheme).upper()
    value = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if not scheme or not value:
        return None
    check = CHECKED_SCHEMES.get(scheme)
    if check is not None and not check(value):
        return None
    return f"{scheme}:{value}"


class RegistrationIds:
    features = frozenset({"reg_id"})
    ref = ComponentRef.of(
        "normalizer", "registration_ids", "1", {"checked": sorted(CHECKED_SCHEMES)}
    )

    def normalize(self, observation: Observation) -> list[Feature]:
        raw = (observation.attributes or {}).get("registration_ids")
        ids = [r for r in (registration_id(v) for v in _values(raw)) if r is not None]
        return _features("reg_id", ids)


# --- domains ------------------------------------------------------------------------------


def registrable_domain(raw: str) -> str | None:
    text = raw.strip().casefold()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    host = re.split(r"[/?#:]", text, maxsplit=1)[0].strip(".")
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9-]+", label) for label in labels):
        return None
    keep = 3 if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES and len(labels) >= 3 else 2
    domain = ".".join(labels[-keep:])
    return None if domain in FREE_MAIL_DOMAINS else domain


class Domains:
    features = frozenset({"domain"})
    ref = ComponentRef.of(
        "normalizer",
        "organization_domains",
        "1",
        {"free_mail": sorted(FREE_MAIL_DOMAINS), "two_level": sorted(TWO_LEVEL_SUFFIXES)},
    )

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        raw = [*_values(attrs.get("domain")), *_values(attrs.get("website")),
               *_values(attrs.get("email"))]  # fmt: skip
        return _features("domain", [d for d in map(registrable_domain, raw) if d])


# --- location -------------------------------------------------------------------------------


def country_code(raw: str) -> str | None:
    folded = re.sub(r"[^a-z ]", "", _fold(raw)).strip()
    folded = re.sub(r"\s+", " ", folded)
    if folded in COUNTRY_NAMES:
        return COUNTRY_NAMES[folded]
    if re.fullmatch(r"[a-z]{2}", folded):
        return folded.upper()
    return None  # unknown spellings are dropped rather than guessed


class Location:
    features = frozenset({"country", "city", "postal"})
    ref = ComponentRef.of("normalizer", "location", "1", {"countries": COUNTRY_NAMES})

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        out = []
        country = attrs.get("country")
        if isinstance(country, str) and (code := country_code(country)):
            out.append(Feature("country", 0, code))
        city = attrs.get("city")
        if isinstance(city, str) and (folded := re.sub(r"\s+", " ", _fold(city)).strip()):
            out.append(Feature("city", 0, folded))
        postal = attrs.get("postal_code")
        if isinstance(postal, (str, int)) and (code := re.sub(r"[^A-Za-z0-9]", "", str(postal))):
            out.append(Feature("postal", 0, code.upper()))
        return out
