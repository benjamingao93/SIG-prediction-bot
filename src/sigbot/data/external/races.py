"""Race fundamentals, one row per race, kept in data/races.csv.

`sigbot races` rebuilds the file from Wikipedia's 2026 election pages (Cook PVI, incumbent,
whether they're running, their last result, which parties have a nominee). Your `margin_adj`,
`skip` and `notes` columns survive a rebuild.

Columns:
  race          - S-TX, G-GA, H-PA-07
  office        - senate | governor | house
  pvi_d         - Cook PVI, Democratic-positive (D+3 → 3, R+5 → -5)
  inc_party     - D | R | I | blank (open/new seat)
  inc_running   - 1 if the incumbent is on the November ballot
  last_year     - year the incumbent last won (Senate/Governor, and House crossover seats)
  last_margin_d - incumbent's winning margin then, Democratic-positive, ≈ 2·share − 100
  has_d, has_r  - 1 if that party has a nominee
  margin_adj    - YOUR adjustment to the expected Dem margin, in points (candidate quality etc.)
  skip          - 1 to give no opinion on this race
  notes
  d_names, r_names - nominee names, ";"-separated (Senate/Governor); used to match Kalshi markets
  rating_p_d    - forecasters' average P(Democrat wins) from the Wikipedia ratings tables
  rating_n, rating_spread - how many rated it, and max − min of their P(D)
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional

COLUMNS = ["race", "office", "pvi_d", "inc_party", "inc_running", "last_year", "last_margin_d",
           "has_d", "has_r", "margin_adj", "skip", "notes", "d_names", "r_names",
           "rating_p_d", "rating_n", "rating_spread"]
USER_COLUMNS = ("margin_adj", "skip", "notes")

WIKI_RAW = "https://en.wikipedia.org/w/index.php?title={}&action=raw"
PAGES = {
    "house": "2026_United_States_House_of_Representatives_elections",
    "senate": "2026_United_States_Senate_elections",
    "governor": "2026_United_States_gubernatorial_elections",
}

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


@dataclass(frozen=True)
class Race:
    race: str
    office: str
    pvi_d: float
    inc_party: str = ""
    inc_running: bool = False
    last_year: Optional[int] = None
    last_margin_d: Optional[float] = None
    has_d: bool = True
    has_r: bool = True
    margin_adj: float = 0.0
    skip: bool = False
    notes: str = ""
    d_names: str = ""  # nominees, ";"-separated (several for top-four or jungle ballots)
    r_names: str = ""
    rating_p_d: Optional[float] = None  # forecasters' average P(Democrat wins)
    rating_n: int = 0  # how many forecasters rated the race
    rating_spread: Optional[float] = None  # max − min of their P(D)


# ---------- CSV ----------

def _num(v: str) -> Optional[float]:
    v = (v or "").strip()
    return float(v) if v else None


def _flag(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "y")


def load_races(path: Path) -> Dict[str, Race]:
    if not path.exists():
        return {}
    out: Dict[str, Race] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            key = (row.get("race") or "").strip()
            if not key:
                continue
            ly = _num(row.get("last_year", ""))
            out[key] = Race(
                race=key,
                office=row["office"].strip(),
                pvi_d=_num(row.get("pvi_d", "")) or 0.0,
                inc_party=(row.get("inc_party") or "").strip(),
                inc_running=_flag(row.get("inc_running", "")),
                last_year=int(ly) if ly else None,
                last_margin_d=_num(row.get("last_margin_d", "")),
                has_d=_flag(row.get("has_d", "1")),
                has_r=_flag(row.get("has_r", "1")),
                margin_adj=_num(row.get("margin_adj", "")) or 0.0,
                skip=_flag(row.get("skip", "")),
                notes=(row.get("notes") or "").strip(),
                d_names=(row.get("d_names") or "").strip(),
                r_names=(row.get("r_names") or "").strip(),
                rating_p_d=_num(row.get("rating_p_d", "")),
                rating_n=int(_num(row.get("rating_n", "")) or 0),
                rating_spread=_num(row.get("rating_spread", "")),
            )
    return out


def write_races(path: Path, races: Iterable[Race]) -> None:
    def b(x: bool) -> str:
        return "1" if x else "0"

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in sorted(races, key=lambda r: r.race):
            w.writerow([r.race, r.office, f"{r.pvi_d:g}", r.inc_party, b(r.inc_running),
                        r.last_year or "", "" if r.last_margin_d is None else f"{r.last_margin_d:g}",
                        b(r.has_d), b(r.has_r), f"{r.margin_adj:g}" if r.margin_adj else "",
                        "1" if r.skip else "", r.notes, r.d_names, r.r_names,
                        "" if r.rating_p_d is None else f"{r.rating_p_d:.4f}", r.rating_n or "",
                        "" if r.rating_spread is None else f"{r.rating_spread:.4f}"])


def merge_user_columns(fresh: Iterable[Race], existing: Dict[str, Race]) -> List[Race]:
    """Keep the user's margin_adj / skip / notes when rebuilding from the source."""
    out = []
    for r in fresh:
        old = existing.get(r.race)
        if old:
            r = replace(r, margin_adj=old.margin_adj, skip=old.skip or r.skip, notes=old.notes or r.notes)
        out.append(r)
    return out


# ---------- Wikipedia parsing ----------

_PVI = re.compile(r"[Ss]hading PVI\|(D|R|EVEN)(?:\|([\d.]+))?")
_STRIPE = re.compile(r"\{\{Party stripe\|([^}|]+)")
_SHARE = re.compile(r"data-sort-value=\"?(-?[\d.]+)\"?\s*\|\s*[\d.]+%\s*(DFL|[DRI])")


def parse_pvi(text: str) -> Optional[float]:
    m = _PVI.search(text)
    if not m:
        return None
    if m.group(1) == "EVEN":
        return 0.0
    v = float(m.group(2))
    return v if m.group(1) == "D" else -v


def _party_letter(name: str) -> str:
    n = name.lower()
    if "democratic" in n or n.strip() == "dfl":
        return "D"
    if "republican" in n:
        return "R"
    return "I"


def _candidate_parties(text: str) -> set:
    return {_party_letter(p) for p in _STRIPE.findall(text)}


_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")


def candidate_names(row: str) -> Dict[str, List[str]]:
    """Party letter → nominee names, from a row's candidate list, e.g.
    `*{{Party stripe|Democratic Party (US)}}[[Mary Peltola]] (Democratic)<ref ...>`."""
    out: Dict[str, List[str]] = {"D": [], "R": [], "I": []}
    for line in row.split("\n"):
        m = re.search(r"\{\{Party stripe\|([^}|]+)\}\}(.*)", line)
        if not m:
            continue
        text = re.sub(r"<ref[^>]*/>|<ref.*?(?:</ref>|$)", "", m.group(2))
        text = _LINK.sub(lambda x: x.group(1), text)
        text = re.sub(r"\{\{[^}]*\}\}", "", text)
        name = re.sub(r"\s*\([^()]*\)\s*$", "", text.strip()).strip(" '*")  # drop "(Democratic)"
        if name:
            out[_party_letter(m.group(1))].append(name)
    return out


_RATING = re.compile(r"\{\{\s*USRaceRating\s*\|([^}]*)\}\}", re.I)
# Forecaster rating → P(the rated party wins). Tunable priors, not fitted.
RATING_P = {"safe": 0.97, "solid": 0.97, "likely": 0.88, "lean": 0.72, "tilt": 0.60}


def ratings_p_d(row: str) -> List[float]:
    """Each forecaster's rating in a predictions-table row, as P(Democrat wins)."""
    out = []
    for body in _RATING.findall(row):
        parts = [p.strip() for p in body.split("|")]
        level = parts[0].lower()
        if level in ("tossup", "toss-up"):
            out.append(0.5)
            continue
        party = parts[1].upper() if len(parts) > 1 else ""
        if level in RATING_P and party in ("D", "R"):
            out.append(RATING_P[level] if party == "D" else 1 - RATING_P[level])
    return out


def parse_ratings(wiki: str) -> Dict[str, List[float]]:
    """State → forecasters' P(D) from a page's predictions table."""
    out: Dict[str, List[float]] = {}
    for row in _rows(_section(wiki, r"\n==\s*Predictions\s*==")):
        st, ps = _state_of(row), ratings_p_d(row)
        if st and ps:
            out.setdefault(st, ps)
    return out


def _with_names_and_ratings(r: "Race", row: str, ratings: Dict[str, List[float]], st: str) -> "Race":
    names = candidate_names(row)
    ps = ratings.get(st) or []
    return replace(r, d_names=";".join(names["D"]), r_names=";".join(names["R"]),
                   rating_p_d=sum(ps) / len(ps) if ps else None, rating_n=len(ps),
                   rating_spread=(max(ps) - min(ps)) if ps else None)


def _rows(table_text: str) -> List[str]:
    return [r.lstrip("\n") for r in re.split(r"\n\|-[^\n]*", table_text)]


def _incumbent_running(status: str) -> bool:
    s = status.lower()
    if "#dddddd" in s or "retir" in s or "term-limited" in s or "lost" in s or "resign" in s:
        return False
    return any(k in s for k in ("nominated", "advanced to general", "incumbent running",
                                "re-elected", "seeking re-election"))


def parse_generic_ballot(house_wiki: str) -> Optional[float]:
    """Average of the aggregators' generic-ballot margins, Democratic-positive."""
    m = re.search(r"'''Average'''.*?'''(Democrats|Republicans) \+([\d.]+)%'''", house_wiki, re.S)
    if not m:
        return None
    v = float(m.group(2))
    return v if m.group(1) == "Democrats" else -v


def parse_house_crossovers(wiki: str) -> Dict[str, float]:
    """2024 winning margin (Democratic-positive) of members holding the other party's turf:
    the House races where a personal vote matters most. Only these have margins on the page."""
    out = {}
    body = _section(wiki, r"\n==Crossover seats==", r"\n==[^=]")
    for row in _rows(body):
        m = re.match(r"!\s*\[\[[^\]|]*\|([A-Za-z ]+?) (\d+|at-large)\]\]", row)
        u = re.match(r"!\s*\{\{ushr\|(\w\w)\|(\w+)\|X\}\}", row)
        if m and m.group(1) in STATES:
            st, d = STATES[m.group(1)], m.group(2)
        elif u:
            st, d = u.group(1), u.group(2)
        else:
            continue
        key = f"H-{st}-{d.zfill(2) if d.isdigit() else 'AL'}"
        shades = _PVI.findall(row)
        # PVI, presidential margin, incumbent margin. A fourth shading (old and new PVI) or the
        # "elected to a previous ... version" note means redrawn lines: the 2024 margin was won
        # on a different map and says nothing about the personal vote on this one.
        if len(shades) == 3 and "elected to a previous" not in row:
            party, v = shades[-1]
            out[key] = 0.0 if party == "EVEN" else (float(v) if party == "D" else -float(v))
    return out


def parse_house(wiki: str) -> List[Race]:
    crossover = parse_house_crossovers(wiki)
    start, end = wiki.find("\n==Alabama=="), wiki.find("\n==Non-voting")
    body = wiki[start:end if end > 0 else None]
    out = []
    for row in _rows(body):
        m = re.match(r"!\s*(?:rowspan=\d+\s*\|)?\s*\{\{ushr\|(\w\w)\|(\w+)\|X\}\}", row)
        if not m:
            continue
        st, dist = m.group(1), m.group(2)
        dist = dist.zfill(2) if dist.isdigit() else dist.upper()
        pvi = parse_pvi(row)
        if pvi is None:
            continue
        cells = row.split("\n|")
        inc = ""
        for c in cells[2:5]:
            last = c.rsplit("|", 1)[-1].strip()
            if last in ("Democratic", "DFL"):
                inc = "D"
            elif last == "Republican":
                inc = "R"
            elif last == "Independent":
                inc = "I"
        status = cells[5] if len(cells) > 5 and inc else ""
        parties = _candidate_parties(row)
        key = f"H-{st}-{dist}"
        out.append(Race(key, "house", pvi, inc, bool(inc) and _incumbent_running(status),
                        last_year=2024 if key in crossover else None, last_margin_d=crossover.get(key),
                        has_d="D" in parties, has_r="R" in parties))
    return out


def _state_of(row: str) -> Optional[str]:
    m = re.match(r"!\s*\[\[[^\]|]*\|([^\]]+)\]\]", row)
    if not m:
        return None
    name = re.sub(r"\s*\(special\)", "", m.group(1)).strip()
    return STATES.get(name)


def _section(wiki: str, heading_re: str, end_re: str = r"\n\|\}") -> str:
    m = re.search(heading_re, wiki)
    if not m:
        return ""
    rest = wiki[m.end():]
    e = re.search(end_re, rest)
    return rest[:e.start()] if e else rest


def parse_state_pvis(wiki: str) -> Dict[str, float]:
    """State → PVI from a page's predictions table."""
    out = {}
    for row in _rows(_section(wiki, r"\n==\s*Predictions\s*==")):
        st, pvi = _state_of(row), parse_pvi(row)
        if st and pvi is not None:
            out.setdefault(st, pvi)
    return out


def _last_result(row: str) -> Optional[float]:
    m = _SHARE.search(row)
    if not m:
        return None
    # Only the winner's share is listed. 2·share − 100 is the margin in a two-way race; with
    # strong third parties a plurality winner would come out negative, so floor it at +1.
    margin = max(2 * abs(float(m.group(1))) - 100, 1.0)
    return -margin if m.group(2) == "R" else margin


def _inc_party(row: str) -> str:
    m = re.search(r"\{\{[Pp]arty shading/(?:Text/)?(Democratic|DFL|Republican|Independent)\}\}\s*\|", row)
    return _party_letter(m.group(1)) if m else ""


_STATUS = re.compile(r"Incumbent|[Aa]ppointee|[Rr]etir|Term-limited|[Ll]ost")


def parse_senate(wiki: str) -> List[Race]:
    """Regular elections plus the specials (OH, FL), which sit in their own table."""
    pvis = parse_state_pvis(wiki)
    ratings = parse_ratings(wiki)
    body = (_section(wiki, r"=== Special elections during the preceding Congress ===")
            + _section(wiki, r"=== Elections leading to the next Congress ==="))
    out = []
    for row in _rows(body):
        st = _state_of(row)
        if not st:
            continue
        pvi = parse_pvi(row)
        pvi = pvis.get(st) if pvi is None else pvi
        cells = row.split("\n|")
        years = [int(y) for y in re.findall(r"\[\[(\d{4})[–-]?\d* United States Senate", cells[4] if len(cells) > 4 else "")]
        status = next((c for c in cells[5:] if _STATUS.search(c)), "")
        parties = _candidate_parties(row)
        r = Race(f"S-{st}", "senate", pvi or 0.0, _inc_party(row), _incumbent_running(status),
                 last_year=max(years) if years else None, last_margin_d=_last_result(row),
                 has_d="D" in parties, has_r="R" in parties)
        out.append(_with_names_and_ratings(r, row, ratings, st))
    return out


def parse_governor(wiki: str) -> List[Race]:
    pvis = parse_state_pvis(wiki)
    ratings = parse_ratings(wiki)
    body = _section(wiki, r"=== States ===")
    out = []
    for row in _rows(body):
        st = _state_of(row)
        if not st or st not in pvis:
            continue
        cells = row.split("\n|")
        status = cells[5] if len(cells) > 5 else ""
        # Governors were last elected in 2022, except two-year terms in NH and VT.
        parties = _candidate_parties(row)
        r = Race(f"G-{st}", "governor", pvis[st], _inc_party(row), _incumbent_running(status),
                 last_year=2024 if st in ("NH", "VT") else 2022, last_margin_d=_last_result(row),
                 has_d="D" in parties, has_r="R" in parties)
        out.append(_with_names_and_ratings(r, row, ratings, st))
    return out


def fetch_page(name: str) -> str:
    import httpx

    r = httpx.get(WIKI_RAW.format(PAGES[name]), follow_redirects=True, timeout=30,
                  headers={"User-Agent": "sigbot/0.1 (prediction market research)"})
    r.raise_for_status()
    return r.text
