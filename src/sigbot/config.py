"""Settings loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
class ArbConfig:
    min_profit: float = 0.005  # locked profit per basket set, in SUSQies: one tick
    max_sets: int = 2000  # per order
    max_capital: float = 50_000.0  # most that may sit in baskets at once (cost basis)
    allow_yes: bool = False  # YES baskets assume one listed party wins: not riskless
    exit_enabled: bool = True  # sell held NO baskets when that beats holding to settlement
    exit_early: bool = True  # ...or as soon as selling locks in exit_min_profit over cost
    exit_min_profit: float = 0.0025  # per set, over cost: low, so capital comes back early
    poll_seconds: float = 4.0
    basket_cooldown: float = 10.0  # let the book refresh after trading a basket
    order_ttl: int = 15  # seconds an order may wait at the exchange before it expires unexecuted.
    # Short on purpose: an order the exchange reaches late is priced off old books, which is when
    # one leg fills and another misses (a repair). Expiring unfilled is the safe outcome.
    depth_fraction: float = 0.5  # use at most this share of each shown book level, so a leg
    # still fills if someone takes part of the level first
    repair_ttl: int = 120  # repairs are capped at break-even, so they can wait out a slow exchange
    repair_slippage: float = 0.02  # max loss per share accepted to finish hedging a lopsided fill
    max_book_age: float = 2.0  # seconds: skip a trade whose books went stale waiting on the rate limit
    feed: bool = False  # realtime books for watched races: `sigbot arb --feed`
    feed_markets: int = 40  # markets the feed watches (one channel each)
    quoting: bool = False  # passive quotes: `sigbot arb --quote`
    quote_races: int = 3  # races quoted at once
    quote_size: int = 200  # sets per quote: the most a race can be one-sided if its hedge fails
    quote_edge: float = 0.005  # profit per set if a fill is hedged at the prices it was quoted from
    quote_ttl: int = 180  # seconds a quote rests before expiring by itself (orphans clean up)
    quote_min_life: int = 20  # seconds before repricing a quote upward (saves writes)
    quote_writes_per_cycle: int = 4


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    tournament_slug: str
    mode: str
    db_path: Path
    inputs_path: Path
    races_path: Path
    generic_ballot_d: Optional[float]  # national environment for the fundamentals model; None = off
    market_weight: float  # weight on the market price vs your outside view
    kill_switch: Path
    read_budget: int
    write_budget: int
    risk: RiskLimits
    arb: ArbConfig


def _f(name: str, default: Optional[float]) -> Optional[float]:
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
    a = ArbConfig()
    arb = ArbConfig(
        min_profit=_f("ARB_MIN_PROFIT", a.min_profit),
        max_sets=int(_f("ARB_MAX_SETS", a.max_sets)),
        max_capital=_f("ARB_MAX_CAPITAL", a.max_capital),
        allow_yes=os.environ.get("ARB_YES_BASKETS", "").strip().lower() in ("1", "true", "yes"),
        exit_enabled=os.environ.get("ARB_EXIT", "true").strip().lower() in ("1", "true", "yes"),
        exit_early=os.environ.get("ARB_EXIT_EARLY", "true").strip().lower() in ("1", "true", "yes"),
        exit_min_profit=_f("ARB_EXIT_MIN_PROFIT", a.exit_min_profit),
        poll_seconds=_f("ARB_POLL_SECONDS", a.poll_seconds),
        repair_slippage=_f("ARB_REPAIR_SLIPPAGE", a.repair_slippage),
        max_book_age=_f("ARB_MAX_BOOK_AGE", a.max_book_age),
        order_ttl=int(_f("ARB_ORDER_TTL", a.order_ttl)),
        depth_fraction=_f("ARB_DEPTH_FRACTION", a.depth_fraction),
        feed_markets=int(_f("ARB_FEED_MARKETS", a.feed_markets)),
        quote_races=int(_f("ARB_QUOTE_RACES", a.quote_races)),
        quote_size=int(_f("ARB_QUOTE_SIZE", a.quote_size)),
        quote_edge=_f("ARB_QUOTE_EDGE", a.quote_edge),
        quote_ttl=int(_f("ARB_QUOTE_TTL", a.quote_ttl)),
        quote_min_life=int(_f("ARB_QUOTE_MIN_LIFE", a.quote_min_life)),
    )
    return Settings(
        api_key=api_key,
        base_url=os.environ.get("SIG_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        tournament_slug=os.environ.get("SIG_TOURNAMENT_SLUG", "").strip(),
        mode=mode,
        db_path=Path(os.environ.get("DB_PATH", "data/sig.db")),
        inputs_path=Path(os.environ.get("INPUTS_PATH", "data/inputs.csv")),
        races_path=Path(os.environ.get("RACES_PATH", "data/races.csv")),
        generic_ballot_d=_f("GENERIC_BALLOT_D", None),
        market_weight=_f("MARKET_WEIGHT", 0.7),
        kill_switch=Path(os.environ.get("KILL_SWITCH", "KILL")),
        read_budget=int(_f("READ_BUDGET", 80)),
        write_budget=int(_f("WRITE_BUDGET", 25)),
        risk=risk,
        arb=arb,
    )
