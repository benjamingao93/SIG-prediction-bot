import pytest

from sigbot.config import DirectionalConfig
from sigbot.data.external import kalshi as ks
from sigbot.data.external import races as rc
from sigbot.data.external.races import Race
from sigbot.models.fairvalue import edges, fair_value, kalshi_two_party

# ---- Wikipedia: nominee names and forecaster ratings ----

ROW = """! [[2026 United States Senate election in Alaska|Alaska]]
| {{Shading PVI|R|6}}
<!--Cook-->      | {{USRaceRating|Tossup}}
<!--DDHQ-->      | {{USRaceRating|Lean|D|flip}}
<!--Economist--> | {{USRaceRating|Likely|R}}
<!--Sabato-->    | {{USRaceRating|Safe|R}}
| nowrap | {{Plainlist |
*{{Party stripe|Democratic Party (US)}}[[Mary Peltola]] (Democratic)<ref name="AK2026" />
*{{Party stripe|Republican Party (US)}}[[Dan Sullivan (U.S. senator)|Dan S. Sullivan]] (Republican)<ref>{{cite web |title=x}}</ref>
*{{Party stripe|Republican Party (US)}}Gerald Heikes (Republican)
*{{Party stripe|Minnesota Democratic–Farmer–Labor Party}}[[Peggy Flanagan]] (DFL)
}}"""


def test_candidate_names_by_party():
    n = rc.candidate_names(ROW)
    assert n["D"] == ["Mary Peltola", "Peggy Flanagan"]
    assert n["R"] == ["Dan S. Sullivan", "Gerald Heikes"]


def test_ratings_become_p_democrat():
    assert rc.ratings_p_d(ROW) == pytest.approx([0.5, 0.72, 1 - 0.88, 1 - 0.97])


def test_races_csv_round_trips_the_new_columns(tmp_path):
    p = tmp_path / "races.csv"
    r = Race("S-AK", "senate", -6, d_names="Mary Peltola", r_names="Dan S. Sullivan;Gerald Heikes",
             rating_p_d=0.575, rating_n=10, rating_spread=0.22)
    rc.write_races(p, [r])
    back = rc.load_races(p)["S-AK"]
    assert (back.d_names, back.r_names, back.rating_p_d, back.rating_n, back.rating_spread) == \
        ("Mary Peltola", "Dan S. Sullivan;Gerald Heikes", pytest.approx(0.575), 10, pytest.approx(0.22))


# ---- Kalshi: series, party matching, quotes ----

def test_series_candidates_keep_general_election_only():
    tickers = ["SENATETX", "KXSENATETX", "KXSENATETXR", "KXSENATETXD", "SENATEFLS", "GOVPARTYGA",
               "KXGOVGA", "KXGOVGANOMD", "KXGOVPARTYGA", "KXSENATEDEMLEAD"]
    assert ks.series_candidates(tickers, "S-TX") == ["KXSENATETX", "SENATETX"]
    assert ks.series_candidates(tickers, "S-FL") == ["SENATEFLS"]
    assert ks.series_candidates(tickers, "G-GA") == ["GOVPARTYGA", "KXGOVGA", "KXGOVPARTYGA"]


def test_party_of_labels_and_candidate_names():
    race = Race("S-OR", "senate", 8, d_names="Jeff Merkley", r_names="David Brock Smith")
    assert ks.party_of("Democratic party", race) == "D"
    assert ks.party_of("Republican Party", race) == "R"
    assert ks.party_of("Jeffrey A. Merkley", race) == "D"
    assert ks.party_of("David Brock Smith", race) == "R"
    assert ks.party_of("Some Independent", race) == "O"


def test_quotes_sum_a_partys_candidates():
    race = Race("G-AK", "governor", -6, d_names="Tom Begich", r_names="Dave Bronson;Treg Taylor")
    event = {"markets": [
        {"ticker": "A", "yes_sub_title": "Tom Begich", "yes_bid_dollars": "0.70", "yes_ask_dollars": "0.72", "open_interest_fp": "100"},
        {"ticker": "B", "yes_sub_title": "Dave Bronson", "yes_bid_dollars": "0.10", "yes_ask_dollars": "0.12", "open_interest_fp": "50"},
        {"ticker": "C", "yes_sub_title": "Treg Taylor", "yes_bid_dollars": "0.15", "yes_ask_dollars": "0.17", "open_interest_fp": "50"},
    ]}
    q = {x.party: x for x in ks.quotes_for_event(race, event)}
    assert q["R"].bid == pytest.approx(0.25) and q["R"].ask == pytest.approx(0.29) and q["R"].open_interest == 100
    assert q["D"].mid == pytest.approx(0.71)


def test_discover_picks_the_2026_event_with_most_open_interest():
    class Fake:
        def get(self, path, **kw):
            if kw.get("series_ticker") == "SENATETX":
                return {"events": [{"event_ticker": "SENATETX-26", "title": "Texas Senate (In 2026)",
                                    "markets": [{"open_interest_fp": "5000"}]}]}
            return {"events": [{"event_ticker": "KXSENATETX-24", "title": "Texas Senate 2024",
                                "markets": [{"open_interest_fp": "90000"}]}]}
    m = ks.discover(Fake(), ["S-TX"], series_tickers=["SENATETX", "KXSENATETX"])
    assert m["S-TX"]["event_ticker"] == "SENATETX-26"


def test_manual_map_rows_survive_rediscovery():
    fresh = {"S-TX": {"event_ticker": "NEW"}, "S-GA": {"event_ticker": "G2"}}
    existing = {"S-TX": {"event_ticker": "PINNED", "manual": True}}
    assert ks.merge_map(fresh, existing)["S-TX"]["event_ticker"] == "PINNED"


# ---- fair value and edges ----

CFG = DirectionalConfig()


def kq(d, r, o=None, oi=5000):
    out = {"D": {"bid": d[0], "ask": d[1], "open_interest": oi}, "R": {"bid": r[0], "ask": r[1], "open_interest": oi}}
    if o:
        out["O"] = {"bid": o[0], "ask": o[1]}
    return out


def test_kalshi_two_party_needs_tight_liquid_d_vs_r_quotes():
    assert kalshi_two_party(kq((.59, .61), (.39, .41)), CFG) == (pytest.approx(0.6), pytest.approx(0.02))
    assert kalshi_two_party(kq((.55, .65), (.39, .41)), CFG) is None  # too wide
    assert kalshi_two_party(kq((.59, .61), (.39, .41), oi=100), CFG) is None  # too thin
    assert kalshi_two_party(kq((.49, .51), (.39, .41), o=(.09, .11)), CFG) is None  # strong third candidate


def test_fair_value_blends_and_falls_back():
    race = Race("S-TX", "senate", -6, rating_p_d=0.5, rating_n=10, rating_spread=0.3)
    both = fair_value(race, kq((.59, .61), (.39, .41)), CFG)
    assert both.sources == "kalshi+ratings" and 0.5 < both.p_d < 0.6
    assert both.uncertainty == pytest.approx(0.02 / 2 + 0.1 / 2)
    only_r = fair_value(race, None, CFG)
    assert only_r.sources == "ratings" and only_r.uncertainty == pytest.approx(0.05 + 0.15)
    assert fair_value(Race("S-X", "senate", 0), None, CFG) is None


def test_edges_buy_no_on_the_other_party():
    fv = fair_value(Race("S-RI", "senate", 8, rating_p_d=0.97, rating_n=7, rating_spread=0.0),
                    kq((.985, .995), (.006, .014)), CFG)
    sig = {"eD": (.93, .94), "eR": (.07, .08)}
    by_view = {e.view: e for e in edges(fv, "eD", "eR", sig, CFG)}
    d = by_view["D"]  # Democrat underpriced → buy NO on the Republican at 1 − .07
    assert d.buy_exchange == "eR" and d.price == pytest.approx(0.93)
    assert d.edge == pytest.approx(fv.p_d - 0.93) and d.agree and d.tradeable
    r = by_view["R"]  # buy NO on the Democrat at 1 − .93: far too dear
    assert r.buy_exchange == "eD" and r.edge < 0 and not r.tradeable


def test_disagreeing_sources_are_flagged():
    fv = fair_value(Race("S-TX", "senate", -6, rating_p_d=0.40, rating_n=10, rating_spread=0.1),
                    kq((.64, .66), (.34, .36)), CFG)
    d = {e.view: e for e in edges(fv, "eD", "eR", {"eD": (.60, .61), "eR": (.40, .41)}, CFG)}["D"]
    assert not d.agree  # Kalshi .65 > price .60, but the ratings' .40 is below it
