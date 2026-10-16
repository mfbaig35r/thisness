"""Person normalizers (design 04 §6.2).

Pure and versioned, like the organization normalizers. The name dictionaries are
small and curated: nicknames and transliteration groups that are common in English
and South Asian, Arabic, Slavic and Spanish names. They are part of the normalizer's
configuration, so extending them is a new version and a feature rebuild.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from thisness.core.components import ComponentRef
from thisness.core.observation import Observation
from thisness.normalize.organization import _features, _fold, _values, country_code, name_parts
from thisness.profiles.base import Feature

HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame", "rev",
                        "fr", "lord", "lady", "hon", "capt", "col", "sgt", "herr", "frau",
                        "sra", "srta", "mme", "mlle"})  # fmt: skip
SUFFIXES = {"jr": "jr", "junior": "jr", "sr": "sr", "senior": "sr", "ii": "ii", "iii": "iii",
            "iv": "iv", "2nd": "ii", "3rd": "iii"}  # fmt: skip
CREDENTIALS = frozenset({"phd", "md", "esq", "cpa", "mba", "dds", "rn", "jd", "pe", "obe", "mbe"})
# Particles that belong to the family name: "Ludwig van Beethoven", "Oscar de la Hoya".
PARTICLES = frozenset({"van", "von", "der", "den", "de", "da", "das", "dos", "del", "della",
                       "di", "du", "la", "le", "bin", "binti", "ibn", "al", "el", "ben", "ter",
                       "st", "mac", "ap"})  # fmt: skip

# Name groups: every spelling in a group is the same given name. The first spelling is
# the group's canonical form. A name can sit in more than one group ("Sam").
NAME_GROUPS: tuple[tuple[str, ...], ...] = (
    # English nicknames
    ("robert", "rob", "bob", "bobby", "robbie", "bert"),
    ("william", "will", "bill", "billy", "willy", "liam"),
    ("elizabeth", "liz", "lizzie", "beth", "betty", "eliza", "libby", "betsy", "elisabeth"),
    ("katherine", "kate", "katie", "kathy", "cathy", "catherine", "kathryn", "kat", "katharine"),
    ("james", "jim", "jimmy", "jamie"),
    ("michael", "mike", "mikey", "mick", "mikhail", "miguel"),
    ("david", "dave", "davy"),
    ("daniel", "dan", "danny"),
    ("thomas", "tom", "tommy"),
    ("joseph", "joe", "joey", "jose", "yusuf", "yousef", "youssef"),
    ("christopher", "chris", "kit"),
    ("matthew", "matt", "matty"),
    ("nicholas", "nick", "nicky", "nikolai", "nicolas"),
    ("anthony", "tony", "antonio"),
    ("stephen", "steven", "steve", "stevie", "stefan"),
    ("samuel", "sam", "sammy"),
    ("samantha", "sam", "sammy"),
    ("alexander", "alex", "sasha", "aleksandr", "aleksander", "oleksandr", "alejandro"),
    ("alexandra", "alex", "sasha", "alexa"),
    ("margaret", "maggie", "meg", "peggy", "margie", "greta"),
    ("jennifer", "jen", "jenny"),
    ("patricia", "pat", "patty", "trish"),
    ("patrick", "pat", "paddy"),
    ("richard", "rick", "rich", "dick", "ricky", "ricardo"),
    ("edward", "ed", "eddie", "ted", "ned"),
    ("charles", "charlie", "chuck", "carlos"),
    ("susan", "sue", "susie", "suzanne"),
    ("deborah", "deb", "debbie", "debra"),
    ("rebecca", "becky", "becca"),
    ("jonathan", "jon", "jonny"),
    ("john", "jack", "johnny", "jon", "juan", "ivan", "johann"),
    ("benjamin", "ben", "benny"),
    ("andrew", "andy", "drew", "andrei", "andres"),
    ("gregory", "greg"),
    ("timothy", "tim", "timmy"),
    ("jeffrey", "jeff", "geoffrey"),
    ("kenneth", "ken", "kenny"),
    ("ronald", "ron", "ronnie"),
    ("donald", "don", "donnie"),
    ("lawrence", "larry", "laurence"),
    ("victoria", "vicky", "tori"),
    ("abigail", "abby", "abbie"),
    ("emily", "em", "emmy"),
    ("olivia", "liv", "livvy"),
    ("sergei", "sergey", "serge", "sergio"),
    ("dmitri", "dmitry", "dima"),
    ("yuri", "yury", "iurii", "yurii"),
    ("natalia", "natalya", "natasha", "natalie"),
    ("ekaterina", "yekaterina", "katya", "katerina"),
    # Transliteration groups
    ("muhammad", "mohammad", "mohammed", "mohamed", "muhammed", "mohamad", "mohd", "mehmet"),
    ("ahmad", "ahmed", "ahmet"),
    ("hussain", "hussein", "husain", "hossain", "husayn", "hassan"),
    ("abdul", "abdel", "abd"),
    ("khalid", "khaled"),
    ("omar", "umar"),
    ("ali", "aly"),
    ("fatima", "fatimah", "fatma"),
    ("aisha", "ayesha", "aysha", "aishah"),
    ("hamza", "hamzah"),
    ("osama", "usama"),
    ("mustafa", "mostafa", "moustafa"),
    ("rashid", "rasheed"),
    ("syed", "sayed", "sayyid", "said"),
    ("zainab", "zaynab"),
)
_GROUP_OF: dict[str, list[str]] = {}
for _group in NAME_GROUPS:
    for _spelling in _group:
        _GROUP_OF.setdefault(_spelling, []).append(_group[0])
# Family-name transliterations.
FAMILY_CANON = {"mohammed": "muhammad", "mohammad": "muhammad", "mohamed": "muhammad",
                "hussein": "hussain", "husain": "hussain", "hossain": "hussain",
                "chowdhury": "choudhury", "chaudhry": "choudhury", "chaudhary": "choudhury",
                "choudhary": "choudhury", "qureshi": "qureshi", "kureshi": "qureshi",
                "rahman": "rahman", "rehman": "rahman", "nguyen": "nguyen", "ngyuen": "nguyen",
                "mueller": "muller", "muller": "muller", "schmidt": "schmidt",
                "schmitt": "schmidt"}  # fmt: skip


def name_groups(given: str) -> list[str]:
    return sorted(_GROUP_OF.get(given, [given]))


def canonical_given(given: str) -> str:
    """One deterministic spelling per given name, for blocking and frequency."""
    return name_groups(given)[0]


def soundex(word: str) -> str:
    """American Soundex: the first letter and three digits."""
    word = re.sub(r"[^a-z]", "", word)
    if not word:
        return ""
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
             **dict.fromkeys("dt", "3"), "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}  # fmt: skip
    out, last = word[0], codes.get(word[0], "")
    for c in word[1:]:
        code = codes.get(c, "")
        if code and code != last:
            out += code
        if c not in "hw":
            last = code
    return (out + "000")[:4]


NAME_ORDERS = ("given_first", "family_first")


def parse_name(raw: str, order: str = "given_first") -> dict[str, Any] | None:
    """'Dr. Mohammad A. Khan Jr.', 'Khan, Mohammad A.' -> given, middle, family, suffix.

    A comma means 'Family, Given ...' unless what follows it is only a suffix or a
    credential ('John Smith, Jr.', 'Jane Doe, PhD'). Without a comma the source's
    ``order`` decides: 'given_first', or 'family_first' as registries write names
    ('SMITH JOHN A JR', design 09 §3). A family-first name of three or more words may
    have a compound family name ('Pineyro Sublett Carla'), which the words alone cannot
    tell from a middle name; ``alt`` is then the other reading, given and family, with
    the family name's last word as family, as a given-first record would parse it."""
    text = _fold(raw).replace("\u2019", "'")
    head, _, tail = text.partition(",")
    tail_tokens = re.findall(r"[a-z0-9']+", tail)
    if tail_tokens and not all(t in SUFFIXES or t in CREDENTIALS for t in tail_tokens):
        text = f"{tail} {head}"  # "Family, Given Middle" -> "Given Middle Family"
    elif order == "family_first":
        return _parse_family_first(text)
    tokens = [t.replace("'", "") for t in re.findall(r"[a-z0-9']+", text)]
    tokens = [t for t in tokens if t]
    while tokens and tokens[0] in HONORIFICS and len(tokens) > 2:
        tokens = tokens[1:]
    suffix = ""
    while len(tokens) > 2 and (tokens[-1] in SUFFIXES or tokens[-1] in CREDENTIALS):
        if tokens[-1] in SUFFIXES:
            suffix = SUFFIXES[tokens[-1]]
        tokens = tokens[:-1]
    if len(tokens) < 2:
        return None
    family_start = len(tokens) - 1
    while family_start > 1 and tokens[family_start - 1] in PARTICLES:
        family_start -= 1
    given = tokens[0]
    middle = "".join(t[0] for t in tokens[1:family_start])
    family = "".join(tokens[family_start:])
    return {"given": given, "middle": middle, "family": FAMILY_CANON.get(family, family),
            "suffix": suffix}  # fmt: skip


def _parse_family_first(text: str) -> dict[str, Any] | None:
    tokens = [t.replace("'", "") for t in re.findall(r"[a-z0-9']+", text)]
    tokens = [t for t in tokens if t and t not in HONORIFICS]
    suffix = ""
    kept = tokens[:1]
    for t in tokens[1:]:  # suffixes stand anywhere: "DAY II ROWLAND W", "YOUNG RONALD JR"
        if t in SUFFIXES or t in CREDENTIALS:
            suffix = SUFFIXES.get(t, suffix)
        else:
            kept.append(t)
    tokens = kept
    family_end = 1
    while family_end < len(tokens) - 1 and tokens[family_end - 1] in PARTICLES:
        family_end += 1  # "VON EMSTER KURT", "DE LA HOYA OSCAR"
    if len(tokens) > 2 and tokens[-1] in PARTICLES:  # "EMSTER KURT VON"
        tokens = [tokens[-1], *tokens[:-1]]
        family_end += 1
    if len(tokens) - family_end < 1:
        return None
    family = "".join(tokens[:family_end])
    given, rest = tokens[family_end], tokens[family_end + 1 :]
    out: dict[str, Any] = {"given": given, "middle": "".join(t[0] for t in rest),
                           "family": FAMILY_CANON.get(family, family), "suffix": suffix}  # fmt: skip
    if family_end == 1 and rest and len(rest[0]) > 1 and len(given) > 1:
        out["alt"] = (rest[0], FAMILY_CANON.get(given, given))
    return out


class PersonName:
    """Features: ``pname.given`` and ``pname.family`` (folded spellings),
    ``pname.groups`` (nickname and transliteration groups of the given name),
    ``pname.middle`` (initials), ``pname.suffix``, ``pname.key`` (canonical given and
    family, the exact-name key used for blocking and frequency), ``pname.tf_key``,
    ``pname.family_initial`` and ``pname.phonetic`` (blocking keys). A family-first name
    that may have a compound family name adds ``pname.alt`` ("given family", the other
    reading); ``pname.reading`` holds the key of every reading, for blocking, and of a
    family-first name's order swapped too, since registries are not always consistent.
    ``pname.given_employer`` (canonical given name and employer) finds family names that
    changed or were mistyped where there is no date of birth.

    The ``name_order`` attribute, set by the source, says how a name without a comma is
    written: ``given_first`` (the default) or ``family_first`` (design 09 §3)."""

    features = frozenset({"pname.given", "pname.family", "pname.groups", "pname.middle",
                          "pname.suffix", "pname.key", "pname.tf_key", "pname.family_initial",
                          "pname.phonetic", "pname.given_dob", "pname.alt",
                          "pname.reading", "pname.given_employer"})  # fmt: skip
    ref = ComponentRef.of(
        "normalizer",
        "person_name",
        "2",
        {"honorifics": sorted(HONORIFICS), "suffixes": SUFFIXES,
         "credentials": sorted(CREDENTIALS), "particles": sorted(PARTICLES),
         "groups": [list(g) for g in NAME_GROUPS], "family_canon": FAMILY_CANON,
         "orders": list(NAME_ORDERS), "compound_alt": "last_word",
         "family_first_swapped_reading": True, "given_employer": "organization_name@2"},
    )  # fmt: skip

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        raw = attrs.get("name")
        order = "given_first"
        if isinstance(raw, str) and raw.strip():
            order = attrs.get("name_order") if attrs.get("name_order") in NAME_ORDERS else order
        else:
            parts = [attrs.get("given_name"), attrs.get("family_name")]
            raw = " ".join(p for p in parts if isinstance(p, str) and p.strip())
        parsed = parse_name(raw, order) if raw else None
        if parsed is None:
            return []
        given, family = parsed["given"], parsed["family"]
        canon = canonical_given(given) if len(given) > 1 else given
        key = f"{canon} {family}"
        out = [
            Feature("pname.given", 0, given),
            Feature("pname.family", 0, family),
            Feature("pname.key", 0, key),
            Feature("pname.tf_key", 0, key),
            Feature("pname.family_initial", 0, f"{family} {given[0]}"),
            Feature("pname.phonetic", 0, f"{soundex(given)} {soundex(family)}"),
        ]
        out += _features("pname.groups", name_groups(given) if len(given) > 1 else [])
        readings = [key]
        if alt := parsed.get("alt"):
            out.append(Feature("pname.alt", 0, f"{alt[0]} {alt[1]}"))
            readings.append(f"{canonical_given(alt[0])} {alt[1]}")
        if order == "family_first" and "," not in raw and len(family) > 1:
            readings.append(f"{canonical_given(family)} {FAMILY_CANON.get(given, given)}")
        out += _features("pname.reading", readings)
        employer = attrs.get("employer")
        if isinstance(employer, str) and employer.strip():
            tokens, _ = name_parts(employer)
            if tokens:
                out.append(Feature("pname.given_employer", 0, f"{canon} {''.join(tokens)}"))
        if parsed["middle"]:
            out.append(Feature("pname.middle", 0, parsed["middle"]))
        if parsed["suffix"]:
            out.append(Feature("pname.suffix", 0, parsed["suffix"]))
        if dob := date_of_birth(attrs.get("date_of_birth"), attrs.get("country")):
            out.append(Feature("pname.given_dob", 0, f"{canon} {dob[0]}"))
        return out


# -- email ------------------------------------------------------------------------------------

# Providers that deliver mail regardless of dots in the local part.
DOTLESS = frozenset({"gmail.com", "googlemail.com"})
# Providers with '+tag' sub-addressing.
PLUS_TAGS = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                       "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
                       "fastmail.com", "yahoo.com"})  # fmt: skip


def email_address(raw: str) -> str | None:
    text = raw.strip().casefold().removeprefix("mailto:")
    if text.count("@") != 1:
        return None
    local, domain = text.split("@")
    domain = domain.strip(".")
    if not local or "." not in domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
        return None
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain in PLUS_TAGS:
        local = local.split("+", 1)[0]
    if domain in DOTLESS:
        local = local.replace(".", "")
    return f"{local}@{domain}" if local else None


class Emails:
    features = frozenset({"email"})
    ref = ComponentRef.of("normalizer", "person_email", "1",
                          {"dotless": sorted(DOTLESS), "plus_tags": sorted(PLUS_TAGS)})  # fmt: skip

    def normalize(self, observation: Observation) -> list[Feature]:
        raw = (observation.attributes or {}).get("email")
        return _features("email", [e for e in map(email_address, _values(raw)) if e])


# -- phone ------------------------------------------------------------------------------------

DIAL_CODES = {"US": "1", "CA": "1", "GB": "44", "DE": "49", "FR": "33", "IN": "91", "PK": "92",
              "AU": "61", "MX": "52", "ES": "34", "IT": "39", "NL": "31", "IE": "353",
              "BR": "55", "CN": "86", "JP": "81", "AE": "971", "SA": "966", "EG": "20",
              "NG": "234", "ZA": "27", "BD": "880", "RU": "7", "UA": "380", "TR": "90"}  # fmt: skip


def phone_number(raw: str, country: str | None) -> str | None:
    """Digits with the country code, E.164 without the plus. National numbers take the
    observation's country; a leading trunk 0 is dropped. Too short: None."""
    text = raw.strip()
    text = re.split(r"\s*(?:x|ext\.?|extension)\s*\d+$", text, flags=re.I)[0]
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        number = digits
    elif digits.startswith("00"):
        number = digits[2:]
    else:
        code = DIAL_CODES.get(country or "")
        if code == "1" and len(digits) == 11 and digits.startswith("1"):
            number = digits
        elif code is None:
            return None if len(digits) < 8 else f"?{digits.lstrip('0')}"
        else:
            number = code + digits.lstrip("0")
    return number if 8 <= len(number) <= 15 else None


class Phones:
    features = frozenset({"phone"})
    ref = ComponentRef.of("normalizer", "person_phone", "1", {"dial_codes": DIAL_CODES})

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        country = attrs.get("country")
        code = country_code(country) if isinstance(country, str) else None
        found = [phone_number(p, code) for p in _values(attrs.get("phone"))]
        return _features("phone", [p for p in found if p])


# -- date of birth ----------------------------------------------------------------------------

DAY_FIRST = frozenset({"GB", "DE", "FR", "IN", "PK", "AU", "IE", "ES", "IT", "NL", "BR", "MX",
                       "NG", "ZA", "BD", "RU", "UA", "TR", "AE", "SA", "EG"})  # fmt: skip


def date_of_birth(raw: object, country: object = None) -> tuple[str, str, str] | None:
    """(full, year-month, year) for a date; full is '' when only part is known.
    ISO dates, 'YYYY-MM' and 'YYYY' are read as written. Slashed dates are month first
    in the US and day first elsewhere, unless one field is over 12."""
    if raw is None:
        return None
    text = str(raw).strip()
    code = country_code(country) if isinstance(country, str) else None
    y = m = d = None
    if mt := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text):
        y, m, d = map(int, mt.groups())
    elif mt := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text):
        a, b, y = map(int, mt.groups())
        if a > 12:
            d, m = a, b
        elif b > 12:
            m, d = a, b
        elif code in DAY_FIRST:
            d, m = a, b
        else:
            m, d = a, b
    elif mt := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text):
        y, m = map(int, mt.groups())
    elif mt := re.fullmatch(r"(\d{4})", text):
        y = int(mt.group(1))
    if y is None or not 1900 <= y <= 2030 or (m is not None and not 1 <= m <= 12):
        return None
    if d is not None:
        try:
            full = date(y, m, d).isoformat()
        except ValueError:
            return None
        return full, full[:7], full[:4]
    if m is not None:
        return "", f"{y:04d}-{m:02d}", f"{y:04d}"
    return "", "", f"{y:04d}"


class DateOfBirth:
    features = frozenset({"dob", "dob.ym", "dob.y"})
    ref = ComponentRef.of("normalizer", "date_of_birth", "1", {"day_first": sorted(DAY_FIRST)})

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        parsed = date_of_birth(attrs.get("date_of_birth"), attrs.get("country"))
        if parsed is None:
            return []
        full, ym, y = parsed
        out = [Feature("dob.y", 0, y)]
        if ym:
            out.append(Feature("dob.ym", 0, ym))
        if full:
            out.append(Feature("dob", 0, full))
        return out


# -- employer, address, identifiers -----------------------------------------------------------


class Employer:
    """The employer's organization name key, so employers compare like organizations."""

    features = frozenset({"employer"})
    ref = ComponentRef.of("normalizer", "employer", "1", {"via": "organization_name@2"})

    def normalize(self, observation: Observation) -> list[Feature]:
        raw = (observation.attributes or {}).get("employer")
        if not isinstance(raw, str) or not raw.strip():
            return []
        tokens, _ = name_parts(raw)
        return [Feature("employer", 0, "".join(tokens))] if tokens else []


STREET = {"street": "st", "st": "st", "avenue": "ave", "ave": "ave", "road": "rd", "rd": "rd",
          "boulevard": "blvd", "blvd": "blvd", "drive": "dr", "dr": "dr", "lane": "ln",
          "ln": "ln", "court": "ct", "ct": "ct", "place": "pl", "pl": "pl", "terrace": "ter",
          "highway": "hwy", "apartment": "apt", "apt": "apt", "unit": "apt", "suite": "ste",
          "ste": "ste", "flat": "apt", "north": "n", "south": "s", "east": "e", "west": "w",
          "strasse": "str", "str": "str"}  # fmt: skip


class Address:
    """A street address key: folded, standard abbreviations, with the postal code."""

    features = frozenset({"address"})
    ref = ComponentRef.of("normalizer", "address", "1", {"street": STREET})

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        raw = attrs.get("address_line")
        if not isinstance(raw, str) or not raw.strip():
            return []
        text = _fold(raw).replace("ß", "ss").replace("#", " apt ")
        tokens = [STREET.get(t, t) for t in re.findall(r"[a-z0-9]+", text)]
        postal = re.sub(r"[^A-Za-z0-9]", "", str(attrs.get("postal_code") or "")).upper()
        return [Feature("address", 0, " ".join(tokens) + (f" {postal}" if postal else ""))]


def person_identifier(raw: str) -> str | None:
    if ":" not in raw:
        return None
    scheme, value = raw.split(":", 1)
    scheme = re.sub(r"[^A-Za-z0-9]", "", scheme).upper()
    value = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return f"{scheme}:{value}" if scheme and value else None


class Identifiers:
    features = frozenset({"ident"})
    ref = ComponentRef.of("normalizer", "person_identifiers", "1", {})

    def normalize(self, observation: Observation) -> list[Feature]:
        raw = (observation.attributes or {}).get("identifiers")
        return _features("ident", [i for i in map(person_identifier, _values(raw)) if i])
