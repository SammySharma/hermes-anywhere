# Strategy Playbook - v3 (Intraday MIS, opening-range breakout on stocks in play)

_The setup lives in `scripts/strategy.py` and the hard limits in `scripts/portfolio.py`. This file explains them._
_It is no longer self-updating: the agent adds lessons at the bottom, but rules change only from `scripts/backtest.py` with the user's approval._
_Last updated: 2026-11-10 (v3 rebuild)_

## Status: hypothesis, not yet validated on NSE

The design follows published research on US stocks. NSE costs are several times higher than the costs in those papers, so the result may not carry over.
Paper trade it, run the backtest on NSE data, and do not use real money until the verdict in `backtest.py run` passes.

## What was wrong with v2

| Flaw | Why it mattered | v3 fix |
|------|-----------------|--------|
| Stops of 0.5% to 1.0% on stocks that move 2% to 3% a day | The stop sat inside ordinary noise, and round-trip costs were 13% to 35% of the planned loss before any edge | Stop is 0.4 of the daily ATR. A cost gate refuses any trade whose costs exceed 25% of its risk |
| Fixed target near 1.5R and stop moved to cost at +1R | With no edge that gives 50% full stops, 17% scratches and 33% targets: zero before costs, about -0.2R after. It also cut off the large winners that momentum setups depend on | No target, no breakeven. Exit at the stop or the square-off |
| Entry after a completed 15-minute bar, a 1 to 3 minute feed delay, an agent turn and a human approval | The breakouts still on offer five minutes later are disproportionately the failing ones. The paper book filled them at the quoted price anyway, so it overstated results | The level is known at 09:30. The user approves it once and the monitor fills it as a stop order when it trades |
| The agent chose the stop and target for each trade | Not reproducible, so it could never be backtested or improved | Levels come from code. The agent can only veto |
| VWAP reclaim and reject on a single bar | A one-bar cross of VWAP happens many times a day in a ranging stock. No evidence was behind it | Removed |
| Index direction from the VWAP side of an ETF | A long-only-in-up-markets tilt fails in ranges and reversals, and the midcap ETF is too thin to trust | Side comes from the stock's own opening candle, so the book is long and short every day |
| Rules edited after 20 trades in a bucket | A 20-trade average has a standard error near 0.25R. With setup, side, segment, time and sector buckets, some bucket always looks bad by luck | Changes come from the backtest across regimes. A live bucket needs 100 trades to count |
| 1% risk on three positions with a 2% daily limit | Three positions on the same side behave like one. A bad open could lose 3% before the 2% halt | 0.5% risk per trade, 1.5% daily limit |
| Shorts in any Nifty 500 stock | A stock with a fixed price band can lock at the upper circuit. The short cannot be covered and goes to auction | Shorts only in F&O stocks |
| Smallcap slots | About 0.3% round-trip cost, a third of a 1% stop | No slots until a backtest supports them |

## The setup

1. **Stocks in play.** At 09:30, keep shortlist names whose 09:15 candle traded at least 2x their own 14-session average volume for that candle. Unusual opening volume marks a stock with news or an order imbalance, and the research finds the opening-range edge concentrated in these names.
2. **Side.** Long if the opening candle closed above its open, short if below.
3. **Entry.** A stop order 0.02% beyond the candle's high (long) or low (short). Skip a stock whose opening candle already spans a full ATR. The order is cancelled at 12:00 if it has not traded.
4. **Stop.** 0.4 of the 14-day ATR from the trigger. Never moved.
5. **Exit.** The stop, or the 15:15 square-off.
6. **Selection.** Rank by opening-range relative volume. Arm at most six, hold at most three, one trade per symbol per day.
7. **Size.** 0.5% of equity at risk including costs, capped by margin and by a third of buying power.

## Bull, bear and neutral markets

No intraday setup earns in every kind of market, and this one does not claim to. It is built so that market direction matters less:

- The side comes from each stock, so a falling market produces shorts and a rising one longs without any switch.
- A stock in play is moving on its own news, which is less tied to what the index does that day.
- Range-bound days are where breakouts fail. The defence is fewer trades (the volume filter often finds nothing), small fixed risk, and the daily halt after three stops.

Whether that is enough is an empirical question. `backtest.py run` labels every day bull, bear or neutral (an equal-weight index against its 50-day average and 20-day return, known before the open) and reports each separately. The verdict fails if any regime loses or has fewer than 30 trades.

## Validation gate

Run `backtest.py fetch --source dhan` (5-minute bars, up to five years) and then `backtest.py run`. The setup passes only if all of these hold after costs:

- At least 200 trades, positive expectancy, t-statistic of 2 or more.
- Positive in the bull, bear and neutral regimes, each with 30 or more trades.
- Positive in both halves of the sample.
- `--sweep` shows neighbouring values of the stop and volume thresholds also positive. One good cell among bad ones is curve fitting.

If it fails, the honest conclusions are, in order: widen the test (more data), simplify, or stop trading this setup. Do not tune until it passes.

## What the backtest cannot tell you

- Fills. A stop order into a breakout slips more on some days than the flat per-fill slippage assumed here. Compare live paper fills with triggers every week.
- Stocks that left the index. The universe is today's constituents, so past results lean slightly favourable.
- Circuit locks and trading halts are not simulated.

## Session map (IST)

| Time | What happens |
|------|--------------|
| 09:15 - 09:30 | Opening range forms |
| 09:33 | Day plan computed, vetoes applied, one approval requested |
| 09:30 - 12:00 | Approved orders fill if their trigger trades. Unfilled orders are cancelled at 12:00 |
| 12:00 - 15:15 | Open positions run to their stop or the square-off. No new orders |
| 15:15 | Everything is squared off |

## Sources

- SEBI, "Analysis of Intraday Trading by Individuals in Equity Cash Segment" (July 2024): 71% of individual intraday traders lost money in FY23, and the most frequent traders did worse.
- Zarattini, Barbon and Aziz, "A Profitable Day Trading Strategy For The U.S. Equity Market" (2024), SSRN 4729284: opening-range breakout pays only in stocks in play, with an ATR-based stop and the exit at the close.
- Zarattini, Aziz and Barbon, "Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)" (2024), SSRN 4824172: intraday momentum with stops that leave the upside open.

## Lessons Learned

_Added by daily and weekly reviews. Observations only; they do not change rules._

| Date | Lesson |
|------|--------|
| - | - |

## Strategy Changelog

| Date | Change | Reason |
|------|--------|--------|
| 2026-10-31 | Converted from a swing strategy to intraday MIS on the Nifty 500 | Starting framework |
| 2026-10-31 | Tagged Nifty 100 / Midcap 150 / Smallcap 250 segments, gave each its own shortlist slots, costs and backdrop | Top-150-by-turnover held only 40 of the Midcap 150 names |
| 2026-11-10 | v3: one code-defined setup (ORB on stocks in play), ATR stop, no target or breakeven, armed stop entries, 0.5% risk, cost gate, F&O-only shorts, backtester with regime report | Audit of v2, see the table above |
