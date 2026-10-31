# Research Principles - v2 (Intraday MIS)

_This file is SELF-UPDATING. Alpha Desk modifies it based on what works and what does not._
_Last updated: 2026-10-31 (intraday conversion)_
_Version: 2_

## Active Principles

### 1. Survive the day first
With 5x leverage, one bad hour can erase a week.
The 1% risk per trade and the -2% daily limit are what keep the desk alive.
No setup is good enough to break them.

**Track record**: New - unvalidated.

### 2. Volume confirms direction
A break on thin volume is a trap.
Require relative volume of at least 1.2, and prefer 1.5 or more.

**Track record**: New - unvalidated.

### 3. Trade with the index
The index against its VWAP sets the day's direction: Nifty 50 for large caps, Midcap 150 for midcaps and smallcaps.
Counter-trend trades need extra evidence: strong relative strength and heavy volume.

**Track record**: New - unvalidated.

### 4. Liquidity is a requirement
Only the shortlist qualifies: high turnover and enough range to reach a 1% to 2% target.
A stock that cannot be exited at the stop is not a trade.

**Track record**: New - unvalidated.

### 5. Structure sets the stop, the rules cap it
Place the stop where the setup is proven wrong, within 0.5% to 1.0%.
If that level is farther away, skip the trade instead of squeezing the stop.

**Track record**: New - unvalidated.

### 6. Costs are a position
Fees and slippage take a fixed bite out of every round trip, and a bigger one in midcaps and smallcaps.
Require net reward:risk of at least 1.5 after costs, and take fewer, better trades.

**Track record**: New - unvalidated.

### 7. Time of day matters
The first hours carry the volume and the trends.
Midday is chop. Let the entry-time statistics decide how strict to be.

**Track record**: New - unvalidated.

### 8. Flat is a position
No setup means no trade.
Six entries a day is a ceiling, not a target.
Log every skipped candidate so the filters can be judged later.

**Track record**: New - unvalidated.

### 9. India is not the US
Results days, RBI policy, index expiry, circuit bands, ASM and GSM lists, and FII and DII flows all change how a stock trades.
Check them before proposing, and never copy a US playbook.

**Track record**: New - unvalidated.

### 10. Every loss gets a one-line post-mortem
Write it when the trade closes, not on Sunday.
Wins get one line too, so the desk learns what works and not only what fails.

**Track record**: New - unvalidated.

## Retired Principles

_None yet. Principles move here when proven wrong over 20 or more trades._

## Principle Changelog

| Date | Change | Reason |
|------|--------|--------|
| 2026-10-31 | Replaced the 10 swing principles with 10 intraday principles | Strategy converted to intraday MIS |

## Evolution Rules

Intraday trades are noisy, so a rule needs a real sample before it changes.

- **Validate**: After 20 or more trades using a principle, mark its track record as "validated" or "questionable".
- **Question**: Negative expectancy in R over 20 or more trades moves a principle to "under review". Test the opposite.
- **Codify**: A new pattern that holds across 20 or more trades becomes a principle.
- **Retire**: Principles that stay wrong move to "Retired" with an explanation.
- **Refine**: Vague principles get sub-rules from specific examples.
- Change one rule at a time. Judge it on the next 20 trades, and revert if it is worse.
- Version-stamp each change: "v3 - added after [date] because [lesson]".
- Hard limits live in `scripts/portfolio.py` and are not edited here. Changing them needs the user's approval.
