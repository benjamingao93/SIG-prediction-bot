"""Settings loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://www.thesuper.market/api/v1"
MODES = ("collect", "paper", "live")


@dataclass(frozen=True)
class RiskLimits:
    max_position: float = 5000.0  # cost basis per exchange
    max_market_exposure: float = 7500.0
    max_group_exposure: float = 15000.0  # correlated races (same group in inputs.csv)
    max_order_cost: float = 2000.0
    daily_loss_stop: float = 10000.0
    kelly_fraction: float = 0.25
    min_edge: float = 0.03
    uncertainty_mult: float = 1.0


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    tournament_slug: str
    mode: str
    db_path: Path
    inputs_path: Path
    kill_switch: Path
    read_budget: int
    write_budget: int
    risk: RiskLimits


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def load_settings(env_file: str = ".env", require_key: bool = True) -> Settings:
    load_dotenv(env_file)
    api_key = os.environ.get("SIG_API_KEY", "").strip()
    if require_key and not api_key:
        raise SystemExit("SIG_API_KEY is not set. Copy .env.example to .env and add your key.")
    mode = os.environ.get("MODE", "paper").strip().lower()
    if mode not in MODES:
        raise SystemExit(f"MODE must be one of {MODES}, got {mode!r}")
    d = RiskLimits()
    risk = RiskLimits(
        max_position=_f("MAX_POSITION", d.max_position),
        max_market_exposure=_f("MAX_MARKET_EXPOSURE", d.max_market_exposure),
        max_group_exposure=_f("MAX_GROUP_EXPOSURE", d.max_group_exposure),
        max_order_cost=_f("MAX_ORDER_COST", d.max_order_cost),
        daily_loss_stop=_f("DAILY_LOSS_STOP", d.daily_loss_stop),
        kelly_fraction=_f("KELLY_FRACTION", d.kelly_fraction),
        min_edge=_f("MIN_EDGE", d.min_edge),
        uncertainty_mult=_f("UNCERTAINTY_MULT", d.uncertainty_mult),
    )
    return Settings(
        api_key=api_key,
        base_url=os.environ.get("SIG_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        tournament_slug=os.environ.get("SIG_TOURNAMENT_SLUG", "").strip(),
        mode=mode,
        db_path=Path(os.environ.get("DB_PATH", "data/sig.db")),
        inputs_path=Path(os.environ.get("INPUTS_PATH", "data/inputs.csv")),
        kill_switch=Path(os.environ.get("KILL_SWITCH", "KILL")),
        read_budget=int(_f("READ_BUDGET", 80)),
        write_budget=int(_f("WRITE_BUDGET", 25)),
        risk=risk,
    )
