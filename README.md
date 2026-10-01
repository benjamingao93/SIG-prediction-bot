# SIG Prediction Bot

Automated trading for the **Susquehanna Predictions Cup — Midterm Elections** (Oct 1 → Nov 4, 2026),
built on the official Super Market API (`https://www.thesuper.market/api/v1`, docs at
<https://sig.thesuper.market/api/v1/docs>).

```
SIG API → market data → probability model → edge vs book → sizing → risk → orders
```

The **probability model** (`models/`) only estimates P(YES) and never sees the order book.
The **trading layer** (`trading/`) never estimates probabilities. Keeping them apart lets you tell afterwards
whether a loss came from the model or from execution.

## Setup

Requires Python ≥ 3.9.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'           # add ,realtime for the WebSocket feed, ,ml for model fitting
cp .env.example .env              # then paste your API key into .env
```

Get a key on the site under **My Profile → API Keys** with the `read` + `trade` scopes. You also need to
confirm your email, register for the tournament and accept the current Terms, or every call returns 403.

## Arbitrage: `sigbot arb`

Each race is listed as one binary market per party, and at most one can resolve YES. When the
YES bids across a race sum above 1, buying NO on every party locks in `Σbid − 1` per set, whoever
wins (`trading/arb.py`). The bot screens all races from bulk prices every few seconds, walks the
books of any race that clears `ARB_MIN_PROFIT`, and buys all legs in one atomic multi-leg order
(`arb_bot.py`). Legs left resting are cancelled, and an uneven fill is evened out with a bounded
repair order; if that fails the race is frozen and logged for you to fix by hand.

YES baskets (asks summing below 1) are off by default: they lose if an unlisted candidate wins.

```bash
sigbot arb            # paper: logs the baskets it would buy
sigbot arb --live     # real orders; also needs MODE=live in .env
```

## Staged rollout

| Stage | Command | Sends orders? |
|---|---|---|
| 0. Verify | `sigbot check`: prints your tournaments; put the slug in `.env` | no |
| 1. Collect | `sigbot collect`: logs prices + books to `data/sig.db`. **Start this first and leave it running.** | no |
| 2. Views | `sigbot races` builds `data/races.csv` for the fundamentals model; set `GENERIC_BALLOT_D`. Check it with `sigbot fundamentals`. Optionally add polls/forecasts: `sigbot markets --inputs`, then fill in `data/inputs.csv` | no |
| 3. Paper | `sigbot run`: logs what it *would* trade to the `signals` table | no |
| 4. Backtest | `sigbot backtest` replays the recorded books; `sigbot report` | no |
| 5. Live | set `MODE=live` in `.env` **and** run `sigbot run --live` | **yes** |

`sigbot run` also collects data, so run *either* `collect` or `run`, never both: the two processes would
share the account's rate limit.

**Emergency stop:** `touch KILL` blocks new orders, and `sigbot cancel-all` pulls every resting order.

## Your inputs: `data/inputs.csv`

| column | meaning |
|---|---|
| `p_poll` | P(YES) from your polling model (`0.72` or `72%`) |
| `p_forecast` | P(YES) from a published forecast |
| `group` | correlation bucket (`senate`, `house-pa`, …); `MAX_GROUP_EXPOSURE` caps each bucket. If blank, the office (`house`/`senate`/`governor`/`chamber`) is used |

If both probabilities are blank, the fundamentals model is the only view on that market. The file is reloaded
every minute while the bot runs.

## Fundamentals model: `data/races.csv`

`sigbot races` pulls every 2026 House, Senate and Governor race from Wikipedia: Cook PVI, the
incumbent's party, whether they're running, their last result, and which parties have a nominee.
`models/fundamentals.py` turns that into a probability:

```
expected Dem margin = beta·2·PVI + gamma·GENERIC_BALLOT_D ± incumbency + margin_adj
P(Dem wins)         = Φ(margin / sqrt(race_sd² + national_sd²))
```

- An incumbent keeps half of how far they beat the expected margin last time (their personal
  vote), or gets a flat bonus if there's no comparable last result.
- Governor races get a smaller weight on the national environment (`gamma` = 0.6) because they
  follow the national mood less.
- The U.S. House and U.S. Senate control markets come from all 435 and 35 races, linked by a
  shared national error.
- Races with an Independent market, or without both a D and an R nominee, are skipped.

The parameters are reasoned priors, not fitted. The model knows nothing about specific candidates,
so the biggest gaps against the market (`sigbot fundamentals`) are usually candidate effects the
market already prices. For each race you have a view on, put your adjustment in `margin_adj`
(points, Dem-positive) or set `skip=1`. Rebuilding keeps those columns, and the bot reloads the
file while it runs. The fundamentals probability counts as one more source next to `p_poll` and
`p_forecast`.

## How a trade is decided

1. `p_c = w·p_market + (1−w)·p_external`, with `w = MARKET_WEIGHT` (0.7) and `p_external` the log-odds average
   of `p_poll`, `p_forecast` and the fundamentals model (`models/ensemble.py`). Swap in fitted logistic
   coefficients once markets settle.
2. Walk the book (`trading/signals.py`): buy YES by lifting asks, or buy NO by hitting YES bids at `1 − bid`. A
   level is only taken while `p_side − price > MIN_EDGE + UNCERTAINTY_MULT·uncertainty`.
3. Size with ¼-Kelly, `f* = (p − q)/(1 − q)` (`trading/sizing.py`).
4. Cap by per-position, per-market and per-group exposure, max order cost, cash, a loss stop and the kill
   switch (`trading/risk.py`).
5. Place a limit order with a 2-minute expiry and an idempotency key (`trading/execution.py`). Each exchange
   has a 90-second cooldown.

## API constraints the design respects

- **100 reads + 30 writes per minute per account**, shared by all your keys. The client throttles itself to
  `READ_BUDGET`/`WRITE_BUDGET` and backs off on 429.
- Order books are YES-normalized. Prices sit on a 0.005 tick between 0.005 and 0.995.
- Every order carries `tournamentId`, otherwise it would trade against the public book and balance.
- `502 ORDER_STATUS_UNKNOWN` is retried with the same idempotency key, so an order is never placed twice.
- `sigbot collect --realtime` uses the Supabase WebSocket feed, which doesn't count against the rate limit.
  It hasn't been tested against the live feed yet.

## Layout

```
src/sigbot/
  config.py            settings from .env
  api/                 client (auth, rate limit, retries), markets, orders, portfolio, realtime
  data/                SQLite store, collector, inputs + races (Wikipedia) loaders
  models/              market prior, fundamentals, external view, ensemble, calibration
  trading/             signals, sizing, risk, execution, arb (relationship violations)
  backtest/engine.py   replay recorded books
  bot.py               main loop
  cli.py               `sigbot` command
tests/                 pytest: book walk, Kelly, ticks, risk caps, client retries, inputs
```

## Roadmap

- Fit the ensemble's logistic coefficients on settled markets (`Ensemble.fit`).
- Trade the engine-reported cross-market violations (`trading/arb.py`, report-only for now).
- Add correlation factors (national environment) on top of the static `group` caps.
- Rest passive quotes inside the spread instead of only taking liquidity.
