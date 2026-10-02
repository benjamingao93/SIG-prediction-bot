import time
from types import SimpleNamespace

from sigbot.dashboard import local_state
from sigbot.data.db import DB


def test_a_bot_stuck_in_a_slow_cycle_shows_as_live(tmp_path):
    db = DB(tmp_path / "t.db")
    s = SimpleNamespace(kill_switch=tmp_path / "KILL")
    db.set_status({"mode": "live", "poll_seconds": 4})
    db.conn.execute("UPDATE bot_status SET ts = '2000-01-01T00:00:00+00:00'")  # last cycle long ago
    db.conn.commit()
    assert local_state(db, s)["status"]["running"] is False  # no alive signal: looks stopped
    db.set_alive(time.time() - 150, 98, "live")  # alive now, 2.5 min into a cycle
    st = local_state(db, s)["status"]
    assert st["running"] is True and st["cycle_running"] >= 150


def test_no_alive_signal_for_a_while_means_stopped(tmp_path):
    db = DB(tmp_path / "t.db")
    s = SimpleNamespace(kill_switch=tmp_path / "KILL")
    db.set_status({"mode": "live", "poll_seconds": 4})
    db.set_alive(None, 5, "live")
    db.conn.execute("UPDATE bot_alive SET ts = '2000-01-01T00:00:00+00:00'")
    db.conn.commit()
    assert local_state(db, s)["status"]["running"] is False
