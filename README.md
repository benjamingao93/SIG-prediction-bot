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
(`arb_bot.py`). A trade is skipped if its books are over `ARB_MAX_BOOK_AGE` (2 s) old by send time,
since rate-limit waits can make them stale. Legs left resting are cancelled. An uneven fill becomes a
repair, saved in the database: every cycle the bot buys what it can of the missing legs from the
current book, up to break-even plus `ARB_REPAIR_SLIPPAGE`, and trades nothing else in that race
until it is hedged. For a gap made any other way: `sigbot hedge "Alaska Senate" --max-price 0.34`.

`sigbot dashboard` serves a read-only view at http://localhost:8050: bot status, alerts, unhedged
races and pending repairs, the races closest to triggering, the bot's orders, and your fills and
positions from the exchange.

Exits (`ARB_EXIT`, on by default): a NO basket you hold pays `k−1` per set at settlement, and
selling it pays `Σ(1 − ask)` now. The bot sells as soon as that locks in `ARB_EXIT_MIN_PROFIT`
(0.0025) per set over what the set cost, taking a smaller profit now and freeing the capital
(`ARB_EXIT_EARLY=false` turns this off). It also sells whenever selling beats holding to settlement
by `ARB_MIN_PROFIT`, which is riskless extra profit.
Holdings come from the exchange's positions, and only races held evenly on every leg count, so
your own manual trades are never touched.

Passive quotes (`--quote`, off by default): instead of waiting for both sides of a race to be
mispriced at once, the bot rests a buy-NO order on one party at `p = (k−1) − Σ other legs' NO asks −
ARB_QUOTE_EDGE`, inside that party's spread. When it fills, the fill becomes a repair, and the bot
buys the other legs' NO right away, paying at most break-even plus `ARB_REPAIR_SLIPPAGE`. Quotes are
pulled the moment a fill would no longer earn the edge, and while a race is being hedged. The risk is
the moment between a fill and its hedge; `ARB_QUOTE_SIZE` (200) bounds it per race. In paper mode a
quote "fills" when a trade prints at its price, which ignores queue position, so paper fill counts
are optimistic.

```bash
sigbot arb --quote           # paper: logs quotes and simulated fills/profit
sigbot arb --live --quote    # real resting orders
sigbot arb --live --feed     # realtime books for the races that matter (combine with --quote)
```

Realtime feed (`--feed`, off by default, needs `pip install -e '.[realtime]'`): the bot keeps up to
`ARB_FEED_MARKETS` (40) markets on the WebSocket feed: races it holds, is repairing or quoting first,
then the ones nearest a trigger. Books for those races come from the feed, current as of now, so
their trades and repairs never wait on the rate limit or get skipped as stale; anything the feed
can't vouch for (disconnected, a missed update, an expired order) falls back to REST. The exchange
sends each update once and may drop the last one on a quiet market without a later gap to show it,
so, as the docs require, every watched market is also reloaded over REST every 90 s and its books
are only trusted within 150 s of the last reload. Once a minute it also checks one feed book against
REST and reloads or reconnects if it lags. It also listens on your
account channel: a pushed fill on one of the bot's quotes wakes the bot to read the fills list and
hedge within about a second, and while that channel is healthy the fills list is only re-read
every 30 s.

On a slow exchange, startup reads retry with long timeouts instead of giving up.

YES baskets (asks summing below 1) are off by default: they lose if an unlisted candidate wins.
Exits capture the same mispricing without that risk, because they only close NO you already hold.

```bash
sigbot arb            # paper: logs the baskets it would buy
sigbot arb --live     # real orders; also needs MODE=live in .env
sigbot arb --live --exit-only   # sell baskets and finish repairs only: frees capital, buys nothing
sigbot dashboard      # http://localhost:8050
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
