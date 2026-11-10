# Strategy Playbook - v3.1 (Intraday MIS: momentum, news and reversal sleeves)

_The setup lives in `scripts/strategy.py` and the hard limits in `scripts/portfolio.py`. This file explains them._
_It is no longer self-updating: the agent adds lessons at the bottom, but rules change only from `scripts/backtest.py` with the user's approval._
_Last updated: 2026-11-10 (v3.1: three sleeves)_

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

## Why three sleeves

One breakout setup depends on days that trend. The desk now runs three setups that earn in different conditions and share one risk budget, so no single kind of day decides the month. None of them is a promise: each must pass its own test, and a sleeve that fails is dropped, not tuned.

### 1. Momentum: `ORB_PLAY`

1. **Stocks in play.** At 09:30, keep shortlist names whose 09:15 candle traded at least 2x their own 14-session average volume for that candle. The research finds the opening-range edge concentrated in these names.
2. **Side.** Long if the opening candle closed above its open, short if below.
3. **Entry.** A stop order 0.02% beyond the candle's high (long) or low (short). Skip a stock whose opening candle already spans a full ATR. The order is cancelled at 12:00 if it has not traded.
4. **Stop and exit.** Stop 0.4 of the 14-day ATR from the trigger, never moved. Exit at the stop or the 15:15 square-off.
5. **Selection and size.** Rank by opening-range relative volume. Arm at most six, hold at most three, one trade per symbol per day. 0.5% of equity at risk, half that on a range day.

### 2. News: `ORB_NEWS`

The same order as above, with one change: when the agent scored the stock's overnight news in the order's direction before the open, 1.5x opening volume is enough, and the order is listed first. An order against its own news score is blocked.

- Why: reading and judging company news is the one thing a language model does better than a rule. Lopez-Lira and Tang found that a model's reading of overnight headlines predicted the next day's return in US stocks, more so in smaller stocks and after bad news.
- Why the price confirmation: the score only lowers the bar. Price still has to break the opening range on volume, so a wrong read costs nothing unless the market agrees with it.
- Why scores close at 09:15: a score written after the open would be shaped by the price action it is supposed to predict.
- Weakness: that edge shrinks as more traders use the same models, and it is US evidence. It cannot be backtested here, because no clean archive of dated Indian news exists and a model already knows how past stories ended. It is tagged separately and judged on 100 or more forward paper trades against `ORB_PLAY`.

### 3. Reversal: `VWAP_FADE`

1. **When.** Range days only, evaluated at 11:30, 12:30 and 13:30.
2. **What.** A stock at least 0.8 ATR away from its session VWAP that was not in play at the open (under 1.5x opening volume).
3. **Trade.** Fade toward VWAP at the market. Stop 0.4 ATR beyond the entry. Target is VWAP. Net reward:risk must be at least 1.5.
4. **The agent's check.** Before proposing, it searches for news behind the move. Any real news is a veto.

- Why: research on intraday reversals finds them stronger in volatile markets and not driven by fundamental news, which points to temporary order pressure that gets paid back. A move on ordinary volume with no news fits that description; a move with news does not.
- Weakness: fading is how a trader gets run over on the day a stock really is repricing. The defences are the volume and news filters, the hard stop, full exclusion on trend days, and one fade per symbol.

### Day mode

At 09:30 the code counts how many shortlist opening candles closed up and how many down. If at least 70% point one way (breadth of 0.4 or more), the day is `trend`: breakouts get full risk and nothing is faded. Otherwise it is `range`: breakouts get half risk and the reversal sleeve is on. The agent has no say in this. A long test of LLM trading agents found them too cautious in bull markets and too aggressive in bear markets, so timing stays in code.

## Bull, bear and neutral markets

No intraday strategy earns in every kind of market. This book is built so that each kind has a sleeve suited to it and none can do serious damage:

| Market | What should work | What limits the damage |
|--------|------------------|------------------------|
| Trending up or down | Breakouts in the trend's direction. Side comes from each stock, so a falling market produces shorts without any switch | - |
| Range-bound | Fades back to VWAP | Breakouts at half risk |
| News-heavy (results season) | News-backed breakouts, in either direction | Fades vetoed where news explains the move |
| Violent reversal days | Little | 0.5% risk per trade, three positions, halt at -1.5% |

Whether this holds is an empirical question. `backtest.py run` labels every day bull, bear or neutral (an equal-weight index against its 50-day average and 20-day return, known before the open) and reports each regime, each sleeve and each sleeve within each regime. Expect flat stretches. The verdict fails if any regime loses or has fewer than 30 trades, or if a sleeve loses on its own over 100 or more trades.

## Validation gate

Run `backtest.py fetch --source dhan` (5-minute bars, up to five years) and then `backtest.py run`. The setup passes only if all of these hold after costs:

- At least 200 trades, positive expectancy, t-statistic of 2 or more.
- Positive in the bull, bear and neutral regimes, each with 30 or more trades.
- Positive in both halves of the sample.
- Each sleeve is positive on its own (`by_setup`, or `--sleeves orb` and `--sleeves fade`).
- `--sweep` shows neighbouring values of the stop and volume thresholds also positive. One good cell among bad ones is curve fitting.
- The news sleeve is outside this gate. It needs 100 forward paper trades with a positive expectancy, and better than `ORB_PLAY` over the same period, before it counts.

If it fails, the honest conclusions are, in order: widen the test (more data), simplify, or stop trading this setup. Do not tune until it passes.

## What the backtest cannot tell you

- Fills. A stop order into a breakout slips more on some days than the flat per-fill slippage assumed here. Compare live paper fills with triggers every week.
- Stocks that left the index. The universe is today's constituents, so past results lean slightly favourable.
- Circuit locks and trading halts are not simulated.
- The fade sleeve is tested without the agent's news veto, and with a fill at the next bar's open standing in for the approval delay. Live results will differ in both directions.

## Session map (IST)

| Time | What happens |
|------|--------------|
| 09:15 - 09:30 | Opening range forms |
| 09:00 | Shortlist built, overnight news scored. Scores close at 09:15 |
| 09:33 | Day classified as trend or range, ORB plan computed, vetoes applied, one approval requested |
| 11:30, 12:30, 13:30 | Range days only: fade candidates checked for news and proposed |
| 09:30 - 12:00 | Approved orders fill if their trigger trades. Unfilled orders are cancelled at 12:00 |
| 12:00 - 15:15 | Open positions run to their stop, target or the square-off. No new ORB orders |
| 15:15 | Everything is squared off |

## Sources

- SEBI, "Analysis of Intraday Trading by Individuals in Equity Cash Segment" (July 2024): 71% of individual intraday traders lost money in FY23, and the most frequent traders did worse.
- Zarattini, Barbon and Aziz, "A Profitable Day Trading Strategy For The U.S. Equity Market" (2024), SSRN 4729284: opening-range breakout pays only in stocks in play, with an ATR-based stop and the exit at the close.
- Zarattini, Aziz and Barbon, "Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)" (2024), SSRN 4824172: intraday momentum with stops that leave the upside open.

- Lopez-Lira and Tang, "Can ChatGPT Forecast Stock Price Movements?" (arXiv 2304.07619): a language model's reading of headlines predicts next-day returns, and the effect declines as adoption rises.
- Miwa, "Short-Term Return Reversals and Intraday Transactions" (SSRN 3174484): intraday moves reverse, more so in volatile markets, and not because of fundamental news.
- Gao, Han, Li and Zhou, "Market Intraday Momentum" (JFE 2018): the first half-hour return predicts the last half-hour. Used here only as support for a one-sided open persisting, not as a trade.
- Li, Kim, Cucuringu and Ma, "Can LLM-based Financial Investing Strategies Outperform the Market in Long Run?" (arXiv 2505.07078): LLM timing strategies fade over longer samples and misjudge regimes.

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
| 2026-11-10 | v3.1: added the news sleeve (pre-open scores), the reversal sleeve (VWAP fades on range days) and the code-defined day mode. Backtester reports each sleeve and regime | A single breakout setup depends on trending days |
