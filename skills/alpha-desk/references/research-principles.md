# Research Principles - v3 (Intraday MIS)

_Alpha Desk records track records here. A principle changes only on backtest evidence and with the user's approval._
_Last updated: 2026-11-10 (v3 rebuild)_
_Version: 3_

## Active Principles

### 1. Survive the day first
With 5x leverage, one bad hour can erase a week.
The 0.5% risk per trade and the -1.5% daily limit are what keep the desk alive.
No setup is good enough to break them.

**Track record**: New - unvalidated.

### 2. Trade only stocks in play
The opening-range edge in the research sits in stocks with unusual opening volume, not in breakouts in general.
Require opening-range volume of at least 2x the stock's own average. No stock in play means no trade.

**Track record**: New - unvalidated.

### 3. The stock sets the side, not the index
Long when the opening candle is up, short when it is down. That keeps the book working in rising and falling markets alike.
`with_index` in the day plan is information for the review, not a filter, until a backtest says otherwise.

**Track record**: New - unvalidated.

### 4. Liquidity is a requirement
Only the shortlist qualifies: high turnover and enough daily range.
A stock that cannot be exited at the stop is not a trade, so shorts are limited to F&O stocks, which cannot lock at a circuit.

**Track record**: New - unvalidated.

### 5. Volatility sets the stop
The stop is a fraction of the stock's daily ATR, so it sits outside that stock's ordinary noise.
It is computed in code and never moved, in either direction.

**Track record**: New - unvalidated.

### 6. Costs are a position
Fees and slippage take a fixed bite out of every round trip, and a bigger one in midcaps.
If they exceed a quarter of the planned loss the stop is too tight for that stock, and the trade is refused.

**Track record**: New - unvalidated.

### 7. Let winners run to the close
A breakout strategy loses often and is paid by a few large days. A fixed target or a stop at cost removes exactly those.
Exits are the stop and the square-off, nothing else.

**Track record**: New - unvalidated.

### 8. Flat is a position
No setup means no trade.
Five entries a day is a ceiling, not a target.
Log every vetoed candidate so the vetoes can be judged later.

**Track record**: New - unvalidated.

### 9. India is not the US
Results days, RBI policy, index expiry, circuit bands, ASM and GSM lists, and FII and DII flows all change how a stock trades.
Check them before proposing, and never copy a US playbook.

**Track record**: New - unvalidated.

### 10. Evidence before rules
A rule enters or changes only when `backtest.py` supports it across bull, bear and neutral regimes and on neighbouring parameter values.
Live trades check the backtest. A handful of them is never a reason to change it.

**Track record**: New - unvalidated.

### 11. Never chase, never improvise
The entry is a level approved in advance. A stock already past it is skipped.
The agent vetoes candidates. It does not choose entries, stops or targets.

**Track record**: New - unvalidated.

## Retired Principles

_None yet. Principles move here when proven wrong over 20 or more trades._

## Principle Changelog

| Date | Change | Reason |
|------|--------|--------|
| 2026-10-31 | Replaced the 10 swing principles with 10 intraday principles | Strategy converted to intraday MIS |
| 2026-11-10 | v3: rewrote principles 2, 3, 5, 6, 7 and 10, added 11 | v2 audit: untested setups, stops inside noise, capped winners, rules tuned on 20 trades |

## Evolution Rules

Intraday trades are noisy: one R of noise per trade puts the standard error of a 20-trade average near 0.25R.

- **Validate**: A principle's track record is judged on 100 or more live trades, and on the backtest first.
- **Change**: Test the alternative in `backtest.py` across regimes and with `--sweep`. Adopt it only if it holds on neighbouring values and the user approves.
- **Retire**: Principles that stay wrong move to "Retired" with an explanation.
- Change one rule at a time and version-stamp it: "v4 - added after [date] because [evidence]".
- Hard limits live in `scripts/portfolio.py` and the setup in `scripts/strategy.py`. Neither is edited from here.
