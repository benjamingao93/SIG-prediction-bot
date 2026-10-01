from datetime import datetime, timezone

import pytest

from sigbot.data.external import races as rc
from sigbot.data.external.inputs import RaceInputs
from sigbot.data.external.races import Race, load_races, merge_user_columns, write_races
from sigbot.models.base import MarketContext
from sigbot.models.external import ExternalModel
from sigbot.models.fundamentals import FundamentalsModel, parse_title


def ctx(title, inputs=None):
    return MarketContext("1", title, 0.5, inputs, None, datetime.now(timezone.utc))


@pytest.mark.parametrize("title,expected", [
    ("Will the Democratic Party win the PA-07 House race?", ("H-PA-07", "D")),
    ("Will the Republican Party win the New Hampshire Senate?", ("S-NH", "R")),
    ("Will the Democratic Party win the Texas Governor?", ("G-TX", "D")),
    ("Will the Republican Party win the U.S. House?", ("CH-HOUSE", "R")),
    ("Will the Independent Party win the Nebraska Senate?", ("S-NE", "I")),
    ("Will it rain?", None),
])
def test_parse_title(title, expected):
    assert parse_title(title) == expected


def test_open_seat_follows_lean_and_environment():
    r = Race("H-XX-01", "house", pvi_d=-4)
    fm = FundamentalsModel({r.race: r}, env=8.0)
    assert fm.margin(r) == pytest.approx(0.0)  # R+4 lean cancels a D+8 environment
    assert fm.p_dem(r) == pytest.approx(0.5)


def test_incumbent_gets_at_least_flat_bonus_and_keeps_personal_vote():
    flat = Race("H-XX-01", "house", 0, "R", True)
    # Won by 12.8 in a seat that should have gone R+2.6: a ~10-point personal vote.
    star = Race("H-XX-02", "house", 0, "R", True, last_year=2024, last_margin_d=-12.8)
    weak = Race("H-XX-03", "house", 0, "R", True, last_year=2024, last_margin_d=1.0)
    fm = FundamentalsModel({}, env=0.0)
    assert fm.margin(flat) == pytest.approx(-2.0)
    assert fm.margin(star) == pytest.approx(-0.5 * (12.8 - 2.6))
    assert fm.margin(weak) == pytest.approx(-2.0)  # an underperformer still gets the flat bonus


def test_party_markets_are_complements_and_gaps_give_no_view():
    rs = {"S-TX": Race("S-TX", "senate", -6),
          "S-NE": Race("S-NE", "senate", -10, has_d=False),
          "S-MT": Race("S-MT", "senate", -10, skip=True)}
    fm = FundamentalsModel(rs, env=8.0)
    d = fm.p_yes("Will the Democratic Party win the Texas Senate?")
    r = fm.p_yes("Will the Republican Party win the Texas Senate?")
    assert d + r == pytest.approx(1.0)
    assert fm.p_yes("Will the Democratic Party win the Nebraska Senate?") is None
    assert fm.p_yes("Will the Republican Party win the Montana Senate?") is None
    assert fm.p_yes("Will the Independent Party win the Texas Senate?") is None


def test_house_control_rises_with_environment():
    rs = {f"H-XX-{i:02d}": Race(f"H-XX-{i:02d}", "house", pvi_d=(i - 217) / 20) for i in range(435)}
    lo = FundamentalsModel(rs, env=-3.0).p_yes("Will the Democratic Party win the U.S. House?")
    hi = FundamentalsModel(rs, env=3.0).p_yes("Will the Democratic Party win the U.S. House?")
    assert lo < 0.5 < hi


def test_external_combines_fundamentals_with_your_inputs():
    r = Race("S-TX", "senate", 0)
    fm = FundamentalsModel({r.race: r}, env=0.0)
    title = "Will the Democratic Party win the Texas Senate?"
    alone = ExternalModel(fm).predict(ctx(title))
    assert alone.p_yes == pytest.approx(0.5) and alone.uncertainty == fm.params.uncertainty
    both = ExternalModel(fm).predict(ctx(title, RaceInputs("1", 0.7, None, "senate")))
    assert 0.5 < both.p_yes < 0.7
    assert ExternalModel().predict(ctx(title)) is None


def test_rebuild_keeps_your_columns(tmp_path):
    p = tmp_path / "races.csv"
    write_races(p, [Race("S-TX", "senate", -6, margin_adj=3.0, notes="strong nominee")])
    merged = merge_user_columns([Race("S-TX", "senate", -5)], load_races(p))
    assert merged[0].pvi_d == -5 and merged[0].margin_adj == 3.0 and merged[0].notes == "strong nominee"


HOUSE_WIKI = """
==Crossover seats==
{|class="wikitable sortable"
|-
![[Pennsylvania's 1st congressional district|Pennsylvania 1]]
|[[Brian Fitzpatrick]]
|{{Party shading/Text/Republican}}
|2016
|{{shading PVI|D|1}}
|{{shading PVI|D|0.3}}
|{{shading PVI|R|12.8}}
|-
![[Alabama's 2nd congressional district|Alabama 2]]
|[[Shomari Figures]]
|{{Party shading/Text/Democratic}}
|2024
|{{shading PVI|D|5}}
|{{shading PVI|R|7}}
|{{shading PVI|R|14.3}}
|{{shading PVI|D|9.2}}
|}
==Generic congressional ballot aggregate polls==
|colspan=2 |'''Average'''
|September 30, 2026
|{{Party shading/Democratic}} |'''Democrats +8.4%'''
==Alabama==
{|class="wikitable sortable"
|-
!rowspan=2 |{{ushr|AL|2|X}}
|rowspan=2 {{shading PVI|R|7}}
|{{sortname|Shomari|Figures}}
|style="color:black;background-color:#B0CEFF" |Democratic
|2024
|Incumbent renominated
|rowspan=2 |{{plainlist}}
*{{Party stripe|Democratic Party (US)}}[[Shomari Figures]] (Democratic)
*{{Party stripe|Republican Party (US)}}[[Rhett Marques]] (Republican)
{{endplainlist}}
|}
==Pennsylvania==
{|class="wikitable sortable"
|-
|-
!{{ushr|PA|1|X}}
|{{shading PVI|D|1}}
|{{sortname|Brian|Fitzpatrick}}
|style="background-color:#FFB6B6" |Republican
|2016
|Incumbent renominated
|{{plainlist}}
*{{Party stripe|Republican Party (US)}}[[Brian Fitzpatrick]] (Republican)
*{{Party stripe|Democratic Party (US)}}Bob Harvie (Democratic)
{{endplainlist}}
|-
!{{ushr|PA|3|X}}
|{{shading PVI|D|39}}
|{{sortname|Dwight|Evans}}
|style="color:black;background-color:#B0CEFF" |Democratic
|2016
|style="background:#DDDDDD" |Incumbent retiring
|{{plainlist}}
*{{Party stripe|Democratic Party (US)}}Sharif Street (Democratic)
{{endplainlist}}
|}
==Non-voting delegates==
"""


def test_parse_house():
    by = {r.race: r for r in rc.parse_house(HOUSE_WIKI)}
    assert set(by) == {"H-AL-02", "H-PA-01", "H-PA-03"}  # doubled row separator before PA-1
    pa1 = by["H-PA-01"]
    assert (pa1.pvi_d, pa1.inc_party, pa1.inc_running, pa1.last_margin_d) == (1, "R", True, -12.8)
    assert by["H-AL-02"].last_margin_d is None  # redrawn: 2024 margin was on other lines
    assert not by["H-PA-03"].inc_running and not by["H-PA-03"].has_r
    assert rc.parse_generic_ballot(HOUSE_WIKI) == pytest.approx(8.4)


SENATE_WIKI = """
== Predictions ==
{|
|-
! [[2026 United States Senate election in Minnesota|Minnesota]]
| {{Shading PVI|D|3}}
|}
=== Special elections during the preceding Congress ===
{| class="wikitable sortable"
|-
! [[2026 United States Senate special election in Ohio|Ohio]]<br />(Class 3)
| {{Shading PVI|R|5}}
| [[Jon Husted]]
| {{Party shading/Republican}} | Republican
| 2025 {{small|(appointed)}}
| data-sort-value=0 | Interim appointee nominated
| nowrap | {{Plainlist |
*{{Party stripe|Democratic Party (US)}}[[Sherrod Brown]] (Democratic)
*{{Party stripe|Republican Party (US)}}[[Jon Husted]] (Republican)
}}
|}
=== Elections leading to the next Congress ===
{| class="wikitable sortable"
|-
! [[2026 United States Senate election in Minnesota|Minnesota]]
| {{Shading PVI|D|3}}
| {{sortname|Tina|Smith}}
| {{Party shading/DFL}} | DFL
| data-sort-value=2018 | [[2018 United States Senate special election in Minnesota|2018]]<br />[[2020 United States Senate election in Minnesota|2020]]
| {{Party shading/DFL}} data-sort-value=-48.7 | 48.7% DFL
| {{Party shading/Hold}} data-sort-value=-1 | Incumbent retiring
| nowrap | {{Plainlist |
*{{Party stripe|Minnesota Democratic–Farmer–Labor Party}}[[Peggy Flanagan]] (DFL)
*{{Party stripe|Republican Party (US)}}Royce White (Republican)
}}
|}
"""


def test_parse_senate():
    by = {r.race: r for r in rc.parse_senate(SENATE_WIKI)}
    oh, mn = by["S-OH"], by["S-MN"]
    assert (oh.pvi_d, oh.inc_party, oh.inc_running, oh.last_year) == (-5, "R", True, None)
    assert (mn.inc_party, mn.inc_running, mn.last_year, mn.has_d) == ("D", False, 2020, True)
    assert mn.last_margin_d == 1.0  # 48.7% plurality: floored at +1, not a negative margin
