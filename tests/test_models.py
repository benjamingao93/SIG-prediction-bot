from datetime import datetime, timezone

import pytest

from sigbot.data.external.inputs import RaceInputs, load_inputs, write_template
from sigbot.api.models import Market
from sigbot.models.base import MarketContext
from sigbot.models.calibration import reliability_table
from sigbot.models.ensemble import Ensemble


def ctx(pm, poll=None, fc=None):
    inp = RaceInputs("m1", poll, fc, "g") if (poll or fc) else None
    return MarketContext("m1", "t", pm, inp, 30.0, datetime.now(timezone.utc))


def test_ensemble_linear_pool_worked_example():
    # .7*.65 + .3*.71 = .668
    est = Ensemble().predict(ctx(.65, poll=.71))
    assert est.p_yes == pytest.approx(.668)


def test_no_view_no_estimate():
    assert Ensemble().predict(ctx(.65)) is None


def test_inputs_roundtrip(tmp_path):
    p = tmp_path / "inputs.csv"
    m = Market("42", "RI Senate", "open", None, None, [], False, [])
    assert write_template(p, [m]) == 1
    assert write_template(p, [m]) == 0  # never duplicates
    text = p.read_text().replace("42,RI Senate,,,,", "42,RI Senate,72%,0.75,senate,")
    p.write_text(text)
    inp = load_inputs(p)["42"]
    assert inp.p_poll == pytest.approx(.72) and inp.p_forecast == .75 and inp.group == "senate"


def test_reliability_table():
    rows = reliability_table([(.05, 0), (.07, 1), (.95, 1)], bins=10)
    assert rows[0]["n"] == 2 and rows[0]["freq_yes"] == .5
