"""A synthetic person benchmark with ground truth (design 04 §6.5).

Deterministic for a seed. The traps, from the research report:

- common names: many different people called John Smith or Maria Garcia;
- married-name changes: earlier records under a maiden name, later ones under a
  married name, often with the same email, phone or date of birth;
- nicknames ("Bob" for Robert) and transliterations ("Mohammed" for Muhammad);
- households: two to four people with one family name, address and landline;
- twins: a household pair with the same date of birth and different given names;
- fathers and sons: the same full name at the same address, with "Jr." on only some
  records and different dates of birth;
- recycled phone numbers: one number used by two unrelated people;
- colleagues, who share an employer; people who move house or change jobs, so a
  different address or employer is weak evidence; sparse records with little more
  than a name;
- typos, name formats ("Last, First", initials, honorifics), date formats and
  partial dates, phone formats, email variants (dots, +tags, case).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from thisness.benchmark.organizations import SYLLABLES, _typo

GENERATOR_VERSION = "1"

GIVEN_M = [
    "james",
    "john",
    "robert",
    "michael",
    "william",
    "david",
    "richard",
    "joseph",
    "thomas",
    "charles",
    "christopher",
    "daniel",
    "matthew",
    "anthony",
    "mark",
    "steven",
    "paul",
    "andrew",
    "joshua",
    "kevin",
    "brian",
    "george",
    "timothy",
    "ronald",
    "jason",
    "edward",
    "jeffrey",
    "ryan",
    "jacob",
    "gary",
    "nicholas",
    "eric",
    "jonathan",
    "stephen",
    "larry",
    "justin",
    "scott",
    "brandon",
    "benjamin",
    "samuel",
    "gregory",
    "alexander",
    "patrick",
    "frank",
    "raymond",
    "jack",
    "dennis",
    "jerry",
    "tyler",
    "aaron",
    "henry",
    "adam",
    "douglas",
    "nathan",
    "peter",
    "zachary",
    "kyle",
    "walter",
    "harold",
    "jeremy",
    "ethan",
    "carl",
    "keith",
    "roger",
    "gerald",
    "sean",
    "arthur",
    "noah",
    "lawrence",
    "jesse",
    "bryan",
    "jordan",
    "albert",
    "dylan",
    "bruce",
    "gabriel",
    "alan",
    "logan",
    "wayne",
    "ralph",
    "roy",
    "eugene",
    "vincent",
    "russell",
    "louis",
    "philip",
    "bradley",
    "muhammad",
    "ahmad",
    "ali",
    "omar",
    "hussain",
    "khalid",
    "yusuf",
    "ibrahim",
    "hamza",
    "sergei",
    "dmitri",
    "yuri",
    "wei",
    "jun",
    "hiroshi",
    "raj",
    "arjun",
    "vikram",
    "amit",
    "carlos",
    "luis",
    "miguel",
    "diego",
]
GIVEN_F = [
    "mary",
    "patricia",
    "jennifer",
    "linda",
    "elizabeth",
    "barbara",
    "susan",
    "jessica",
    "sarah",
    "karen",
    "lisa",
    "nancy",
    "margaret",
    "sandra",
    "ashley",
    "kimberly",
    "emily",
    "donna",
    "michelle",
    "carol",
    "amanda",
    "dorothy",
    "melissa",
    "deborah",
    "stephanie",
    "rebecca",
    "sharon",
    "laura",
    "cynthia",
    "kathleen",
    "amy",
    "angela",
    "shirley",
    "anna",
    "brenda",
    "pamela",
    "emma",
    "nicole",
    "helen",
    "samantha",
    "katherine",
    "christine",
    "rachel",
    "carolyn",
    "janet",
    "maria",
    "heather",
    "diane",
    "ruth",
    "julie",
    "olivia",
    "joyce",
    "virginia",
    "victoria",
    "kelly",
    "lauren",
    "christina",
    "joan",
    "evelyn",
    "judith",
    "megan",
    "andrea",
    "cheryl",
    "hannah",
    "jacqueline",
    "martha",
    "gloria",
    "teresa",
    "sara",
    "madison",
    "frances",
    "janice",
    "jean",
    "abigail",
    "alice",
    "judy",
    "sophia",
    "grace",
    "denise",
    "amber",
    "doris",
    "marilyn",
    "danielle",
    "beverly",
    "isabella",
    "theresa",
    "diana",
    "natalie",
    "charlotte",
    "marie",
    "kayla",
    "alexis",
    "fatima",
    "aisha",
    "zainab",
    "natalia",
    "ekaterina",
    "priya",
    "ananya",
    "mei",
    "yuki",
    "sofia",
    "lucia",
]
COMMON_FAMILY = [
    "smith",
    "johnson",
    "williams",
    "brown",
    "jones",
    "garcia",
    "miller",
    "davis",
    "rodriguez",
    "martinez",
    "hernandez",
    "lopez",
    "gonzalez",
    "wilson",
    "anderson",
    "thomas",
    "taylor",
    "moore",
    "jackson",
    "martin",
    "lee",
    "perez",
    "thompson",
    "white",
    "harris",
    "sanchez",
    "clark",
    "ramirez",
    "lewis",
    "robinson",
    "walker",
    "young",
    "allen",
    "king",
    "wright",
    "scott",
    "torres",
    "nguyen",
    "hill",
    "flores",
    "green",
    "adams",
    "nelson",
    "baker",
    "hall",
    "rivera",
    "campbell",
    "mitchell",
    "carter",
    "roberts",
    "khan",
    "patel",
    "shah",
    "singh",
    "kumar",
    "sharma",
    "gupta",
    "ali",
    "hussain",
    "ahmed",
    "rahman",
    "chowdhury",
    "qureshi",
    "malik",
    "iqbal",
    "siddiqui",
    "muller",
    "schmidt",
    "schneider",
    "fischer",
    "weber",
    "meyer",
    "wagner",
    "becker",
    "hoffmann",
    "ivanov",
    "petrov",
    "smirnov",
    "wang",
    "li",
    "zhang",
    "liu",
    "chen",
    "yang",
    "huang",
    "wu",
    "tanaka",
    "suzuki",
    "kim",
    "park",
    "choi",
    "obrien",
    "murphy",
    "kelly",
    "sullivan",
    "walsh",
]
# Spelling variants used on records (the normalizer folds them).
GIVEN_VARIANTS = {
    "robert": ["bob", "rob", "bobby"], "william": ["bill", "will", "billy"],
    "elizabeth": ["liz", "beth", "betty"], "katherine": ["kate", "katie", "kathy"],
    "james": ["jim", "jimmy"], "michael": ["mike"], "david": ["dave"], "daniel": ["dan", "danny"],
    "thomas": ["tom", "tommy"], "joseph": ["joe"], "christopher": ["chris"], "matthew": ["matt"],
    "nicholas": ["nick"], "anthony": ["tony"], "stephen": ["steve", "steven"],
    "samuel": ["sam"], "samantha": ["sam"], "alexander": ["alex"], "margaret": ["maggie", "peggy"],
    "jennifer": ["jen", "jenny"], "patricia": ["pat", "trish"], "patrick": ["pat"],
    "richard": ["rick", "rich"], "edward": ["ed", "ted"], "charles": ["charlie", "chuck"],
    "susan": ["sue"], "deborah": ["debbie", "deb"], "rebecca": ["becky"], "jonathan": ["jon"],
    "benjamin": ["ben"], "andrew": ["andy", "drew"], "gregory": ["greg"], "timothy": ["tim"],
    "jeffrey": ["jeff"], "ronald": ["ron"], "lawrence": ["larry"], "victoria": ["vicky"],
    "abigail": ["abby"], "john": ["jack", "johnny"],
    "muhammad": ["mohammad", "mohammed", "mohamed", "muhammed"], "ahmad": ["ahmed"],
    "hussain": ["hussein", "husain"], "khalid": ["khaled"], "omar": ["umar"],
    "yusuf": ["yousef", "youssef"], "fatima": ["fatimah"], "aisha": ["ayesha"],
    "sergei": ["sergey"], "dmitri": ["dmitry"], "yuri": ["yury"], "natalia": ["natalya"],
}  # fmt: skip
FAMILY_VARIANTS = {"chowdhury": ["chaudhry", "choudhury"], "hussain": ["hussein"],
                   "muller": ["mueller"], "schmidt": ["schmitt"], "rahman": ["rehman"]}  # fmt: skip
COUNTRIES = [("US", 0.6), ("GB", 0.2), ("DE", 0.08), ("IN", 0.04), ("PK", 0.03), ("CA", 0.05)]
CITIES = {"US": ["Chicago", "Houston", "Phoenix", "Columbus", "Denver", "Atlanta", "Seattle"],
          "GB": ["London", "Leeds", "Bristol", "Glasgow", "Manchester"],
          "DE": ["Berlin", "Hamburg", "Munich", "Cologne"], "IN": ["Mumbai", "Delhi", "Pune"],
          "PK": ["Lahore", "Karachi"], "CA": ["Toronto", "Calgary", "Ottawa"]}  # fmt: skip
COUNTRY_SPELLINGS = {"US": ["US", "USA", "United States"], "GB": ["GB", "UK", "United Kingdom"],
                     "DE": ["DE", "Germany"], "IN": ["IN", "India"], "PK": ["PK", "Pakistan"],
                     "CA": ["CA", "Canada"]}  # fmt: skip
DIAL = {"US": "1", "CA": "1", "GB": "44", "DE": "49", "IN": "91", "PK": "92"}
STREETS = [
    "Oak",
    "Maple",
    "Cedar",
    "Elm",
    "Pine",
    "Washington",
    "Lake",
    "Hill",
    "Park",
    "Main",
    "Church",
    "Station",
    "Victoria",
    "Mill",
    "High",
    "King",
    "Queen",
    "River",
    "Bridge",
]
STREET_TYPES = [("Street", "St"), ("Avenue", "Ave"), ("Road", "Rd"), ("Lane", "Ln"),
                ("Drive", "Dr")]  # fmt: skip
MAIL = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"]
ID_SCHEME = {"US": "SSN", "GB": "NINO", "CA": "SIN"}
KINDS = (
    "normal",
    "common",
    "married",
    "nickname",
    "translit",
    "household",
    "twin",
    "junior",
    "recycled",
)


@dataclass(frozen=True)
class Person:
    person_id: str
    kind: str
    gender: str
    given: str
    middle: str | None
    family: str
    maiden: str | None  # set for married-name changes
    suffix: str | None
    dob: tuple[int, int, int]
    country: str
    city: str
    postal: str
    address: tuple[str, str, str]  # number, street, (long, short) type index as str
    mobile: str
    landline: str | None
    personal_email: str
    employer: str | None
    identifier: str | None
    household: str | None = None
    # Earlier address and employer, used on a person's earlier records.
    previous: tuple[str, str, tuple[str, str, str]] | None = None  # city, postal, address
    previous_employer: str | None = None


@dataclass(frozen=True)
class Record:
    record_id: str
    person_id: str
    attributes: dict


@dataclass
class Benchmark:
    seed: int
    people: list[Person]
    records: list[Record]
    entity_type: str = "person"
    kind: dict[str, str] = field(init=False)
    truth: dict[str, str] = field(init=False)  # record id -> person id
    group: dict[str, str | None] = field(init=False)  # person id -> household, for related pairs

    def __post_init__(self) -> None:
        self.kind = {p.person_id: p.kind for p in self.people}
        self.group = {p.person_id: p.household for p in self.people}
        self.truth = {r.record_id: r.person_id for r in self.records}

    @property
    def config(self) -> dict:
        return {"generator": "people", "version": GENERATOR_VERSION, "seed": self.seed,
                "people": len(self.people), "records": len(self.records)}  # fmt: skip


def _postal(rng: random.Random, city: str) -> str:
    """One of 25 postal codes per city: a postal code covers many people."""
    base = sum(map(ord, city)) * 97 % 70000 + 10000
    return str(base + rng.randrange(25) * 13)


def _zipf(rng: random.Random, items: list[str], s: float = 1.0) -> str:
    weights = [1 / (i + 1) ** s for i in range(len(items))]
    return rng.choices(items, weights)[0]


def _people(rng: random.Random, n: int) -> list[Person]:
    used_rare: set[str] = set()
    mailboxes: set[tuple[str, str]] = set()
    employers = [f"{(rng.choice(SYLLABLES) + rng.choice(SYLLABLES)).capitalize()} "
                 f"{rng.choice(['Logistics', 'Foods', 'Systems', 'Health', 'Bank', 'Retail'])}"
                 for _ in range(max(10, n // 12))]  # fmt: skip
    people: list[Person] = []

    def rare_family() -> str:
        while True:
            name = "".join(rng.choice(SYLLABLES) for _ in range(rng.choice((2, 2, 3))))
            if name not in used_rare and len(name) > 4:
                used_rare.add(name)
                return name

    def country() -> str:
        return rng.choices([c for c, _ in COUNTRIES], [w for _, w in COUNTRIES])[0]

    def phone(c: str) -> str:
        return DIAL[c] + str(rng.randrange(2, 10)) + "".join(rng.choice("0123456789")
                                                             for _ in range(9))  # fmt: skip

    def address(c: str) -> tuple[str, str, str]:
        return (str(rng.randrange(1, 9999)), rng.choice(STREETS),
                str(rng.randrange(len(STREET_TYPES))))  # fmt: skip

    def add(kind: str, *, gender=None, given=None, family=None, c=None, city=None, postal=None,
            addr=None, landline=None, dob=None, maiden=None, suffix=None, household=None,
            mobile=None, middle="auto") -> Person:  # fmt: skip
        gender = gender or rng.choice("mf")
        given = given or _zipf(rng, GIVEN_M if gender == "m" else GIVEN_F, 0.6)
        if family is None:
            family = _zipf(rng, COMMON_FAMILY, 0.8) if rng.random() < 0.35 else rare_family()
        c = c or country()
        dob = dob or (rng.randrange(1940, 2005), rng.randrange(1, 13), rng.randrange(1, 29))
        local = rng.choice([f"{given}.{family}", f"{given}{family}", f"{given[0]}{family}",
                            f"{given}{rng.randrange(10, 99)}"])  # fmt: skip
        domain = rng.choice(MAIL)
        while (local.replace(".", ""), domain) in mailboxes:  # one mailbox, one person
            local = f"{local}{rng.randrange(10)}"
        mailboxes.add((local.replace(".", ""), domain))
        employer = rng.choice(employers) if rng.random() < 0.7 else None
        if middle == "auto":
            middle = _zipf(rng, GIVEN_M if gender == "m" else GIVEN_F) if rng.random() < 0.5 \
                else None  # fmt: skip
        p = Person(
            person_id=f"per{len(people):05d}", kind=kind, gender=gender, given=given,
            middle=middle, family=family, maiden=maiden, suffix=suffix, dob=dob, country=c,
            city=(city := city or rng.choice(CITIES[c])), postal=postal or _postal(rng, city),
            address=addr or address(c), mobile=mobile or phone(c), landline=landline,
            personal_email=f"{local}@{domain}".replace(" ", ""), employer=employer,
            identifier=(f"{ID_SCHEME[c]}:{rng.randrange(10**8, 10**9)}"
                        if c in ID_SCHEME and rng.random() < 0.6 else None),
            household=household,
            previous=None,
            previous_employer=(rng.choice(employers) if employer and rng.random() < 0.25
                               else None),
        )  # fmt: skip
        if household is None and rng.random() < 0.2:
            moved_city = rng.choice(CITIES[c])
            p = replace(p, previous=(moved_city, _postal(rng, moved_city), address(c)))
        people.append(p)
        return p

    scale = n / 1000
    counts = {"common": round(40 * scale), "married": round(60 * scale),
              "nickname": round(80 * scale), "translit": round(40 * scale),
              "households": round(40 * scale), "twins": round(10 * scale),
              "juniors": round(15 * scale), "recycled": round(15 * scale)}  # fmt: skip
    for _ in range(counts["common"]):
        g = rng.choice("mf")
        add("common", gender=g, given=rng.choice(["john", "james", "michael"] if g == "m"
                                                 else ["maria", "mary", "jennifer"]),
            family=rng.choice(["smith", "johnson", "garcia"]))  # fmt: skip
    for _ in range(counts["married"]):
        add("married", gender="f", maiden=_zipf(rng, COMMON_FAMILY, 0.8) if rng.random() < 0.4
            else rare_family())  # fmt: skip
    for _ in range(counts["nickname"]):
        g = rng.choice("mf")
        pool = [x for x in (GIVEN_M if g == "m" else GIVEN_F) if x in GIVEN_VARIANTS
                and x not in ("muhammad", "ahmad", "hussain", "khalid", "omar", "yusuf",
                              "fatima", "aisha", "sergei", "dmitri", "yuri", "natalia")]  # fmt: skip
        add("nickname", gender=g, given=rng.choice(pool))
    for _ in range(counts["translit"]):
        g = rng.choice("mf")
        given = rng.choice(["muhammad", "ahmad", "hussain", "khalid", "omar", "yusuf"] if g == "m"
                           else ["fatima", "aisha", "natalia"])  # fmt: skip
        family = rng.choice(["chowdhury", "hussain", "rahman", "khan", "ali", "qureshi"])
        add("translit", gender=g, given=given, family=family,
            c=rng.choice(["GB", "PK", "US", "IN"]))  # fmt: skip
    for h in range(counts["households"]):
        c = country()
        landline = phone(c)
        head = add("household", c=c, landline=landline, household=f"h{h:04d}")
        taken = {head.given}
        for _ in range(rng.randint(1, 3)):
            g = rng.choice("mf")
            given = rng.choice([x for x in (GIVEN_M if g == "m" else GIVEN_F) if x not in taken])
            taken.add(given)
            add("household", gender=g, given=given, family=head.family, c=head.country, city=head.city,
                postal=head.postal, addr=head.address, landline=landline,
                household=f"h{h:04d}")  # fmt: skip
    for t in range(counts["twins"]):
        a = add("twin", household=f"t{t:04d}")
        g = rng.choice("mf")
        given = rng.choice([x for x in (GIVEN_M if g == "m" else GIVEN_F) if x != a.given])
        add("twin", gender=g, given=given, family=a.family, c=a.country, city=a.city,
            postal=a.postal, addr=a.address, dob=a.dob, household=f"t{t:04d}")  # fmt: skip
    for j in range(counts["juniors"]):
        father = add("junior", gender="m", suffix="Sr", household=f"j{j:04d}")
        son_dob = (father.dob[0] + rng.randint(22, 38), rng.randrange(1, 13), rng.randrange(1, 29))
        add("junior", gender="m", given=father.given, middle=father.middle, family=father.family,
            c=father.country, city=father.city, postal=father.postal, addr=father.address,
            dob=(min(son_dob[0], 2006), son_dob[1], son_dob[2]), suffix="Jr",
            household=f"j{j:04d}")  # fmt: skip
    for _ in range(counts["recycled"]):
        a = add("recycled")
        add("recycled", c=a.country, mobile=a.mobile)
    while len(people) < n:
        add("normal")
    return people


def _name(p: Person, rng: random.Random, family: str) -> str:
    given = p.given
    if p.kind in ("nickname", "translit") and given in GIVEN_VARIANTS and rng.random() < 0.5:
        given = rng.choice(GIVEN_VARIANTS[given])
    if p.kind == "translit" and family in FAMILY_VARIANTS and rng.random() < 0.4:
        family = rng.choice(FAMILY_VARIANTS[family])
    if rng.random() < 0.06:
        given = _typo(given, rng)
    elif rng.random() < 0.05:
        family = _typo(family, rng)
    g, f = given.capitalize(), family.capitalize()
    middle = p.middle.capitalize() if p.middle else None
    r = rng.random()
    if r < 0.45:
        name = f"{g} {f}"
    elif r < 0.6 and middle:
        name = f"{g} {middle[0]}. {f}" if rng.random() < 0.7 else f"{g} {middle} {f}"
    elif r < 0.75:
        name = f"{f}, {g}" + (f" {middle[0]}." if middle and rng.random() < 0.5 else "")
    elif r < 0.82:
        name = f"{g[0]}. {f}"
    elif r < 0.88:
        name = (
            f"{rng.choice(['Mr.', 'Dr.'] if p.gender == 'm' else ['Ms.', 'Mrs.', 'Dr.'])} {g} {f}"
        )
    else:
        name = f"{g} {f}"
    if p.suffix and rng.random() < 0.5 and "," not in name:
        name += f" {p.suffix}."
    return name.upper() if rng.random() < 0.12 else name


def _dob(p: Person, rng: random.Random) -> str:
    y, m, d = p.dob
    if rng.random() < 0.02 and d <= 12 and d != m:
        m, d = d, m  # a day and month swapped by hand
    r = rng.random()
    if r < 0.5:
        return f"{y:04d}-{m:02d}-{d:02d}"
    if r < 0.88:
        return f"{m:02d}/{d:02d}/{y:04d}" if p.country == "US" else f"{d:02d}/{m:02d}/{y:04d}"
    return f"{y:04d}-{m:02d}"


def _phone(number: str, country: str, rng: random.Random) -> str:
    code = DIAL[country]
    national = number[len(code) :]
    if country in ("US", "CA"):
        a, b, c = national[:3], national[3:6], national[6:]
        return rng.choice([f"({a}) {b}-{c}", f"{a}-{b}-{c}", f"+1 {a} {b} {c}", f"{a}.{b}.{c}"])
    return rng.choice([f"0{national[:4]} {national[4:]}", f"+{code} {national[:4]} {national[4:]}",
                       f"00{code}{national}"])  # fmt: skip


def _email(address: str, rng: random.Random) -> str:
    local, domain = address.split("@")
    if domain == "gmail.com" and rng.random() < 0.3:
        local = local.replace(".", "") if "." in local else local[:2] + "." + local[2:]
    if domain in ("gmail.com", "outlook.com") and rng.random() < 0.15:
        local += f"+{rng.choice(['shop', 'news', 'work'])}"
    text = f"{local}@{domain}"
    return text.capitalize() if rng.random() < 0.1 else text


def _record(p: Person, rng: random.Random, record_id: str, index: int, total: int) -> Record:
    early = index < total // 2
    family = p.maiden if p.maiden and early else p.family
    city, postal, home = p.city, p.postal, p.address
    if p.previous and early:
        city, postal, home = p.previous
    employer = p.previous_employer if p.previous_employer and early else p.employer
    attributes: dict = {"name": _name(p, rng, family)}
    sparse = rng.random() < 0.15
    if not sparse:
        if rng.random() < 0.55:
            attributes["date_of_birth"] = _dob(p, rng)
        if rng.random() < 0.5:
            emails = [_email(p.personal_email, rng)]
            if employer and rng.random() < 0.3:
                company = employer.split()[0].lower()
                emails.append(f"{p.given}.{family}@{company}.com")
            attributes["email"] = emails
        phones = []
        if rng.random() < 0.45:
            phones.append(_phone(p.mobile, p.country, rng))
        if p.landline and rng.random() < 0.5:
            phones.append(_phone(p.landline, p.country, rng))
        if phones:
            attributes["phone"] = phones
        if employer and rng.random() < 0.35:
            attributes["employer"] = employer + rng.choice(["", " Inc", " Ltd", ""])
        if rng.random() < 0.4:
            number, street, t = home
            long, short = STREET_TYPES[int(t)]
            attributes["address_line"] = f"{number} {street} {rng.choice([long, short])}"
            attributes["postal_code"] = postal
        if p.identifier and rng.random() < 0.15:
            attributes["identifiers"] = [p.identifier]
    if rng.random() < 0.65:
        attributes["city"] = city
    if rng.random() < 0.7:
        attributes["country"] = rng.choice(COUNTRY_SPELLINGS[p.country])
    return Record(record_id, p.person_id, attributes)


def generate(n_people: int = 1000, seed: int = 20261007) -> Benchmark:
    rng = random.Random(seed)
    people = _people(rng, n_people)
    records: list[Record] = []
    for p in people:
        n = min(1 + int(rng.expovariate(0.6)), 7)
        if p.maiden or p.previous or p.previous_employer:
            n = max(n, 2)
        for i in range(n):
            records.append(_record(p, rng, f"rec{len(records):06d}", i, n))
    return Benchmark(seed, people, records)
