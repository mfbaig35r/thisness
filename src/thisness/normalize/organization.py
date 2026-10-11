"""Organization normalizers (design 03 §4).

Each normalizer is pure and versioned. Its dictionaries are part of its
configuration hash, so editing a dictionary yields a new component version and
a feature rebuild, never a silent change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

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
                          "name.trigram", "legal_form"})  # fmt: skip
    ref = ComponentRef.of(
        "normalizer",
        "organization_name",
        "2",
        {"legal_forms": LEGAL_FORMS, "token_canon": TOKEN_CANON,
         "acronym_stopwords": sorted(ACRONYM_STOPWORDS), "confusables": CONFUSABLES},
    )  # fmt: skip

    def normalize(self, observation: Observation) -> list[Feature]:
        raw = (observation.attributes or {}).get("name")
        if not isinstance(raw, str) or not raw.strip():
            return []
        tokens, legal = name_parts(raw)
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
