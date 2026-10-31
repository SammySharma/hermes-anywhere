# Strategy Playbook - v2 (Intraday MIS, Nifty 500)

_This file is SELF-UPDATING. Alpha Desk edits setups, filters and time-of-day rules based on results._
_Hard limits (risk per trade, stop range, leverage, position and trade caps, loss limit, square-off) live in `scripts/portfolio.py`, not here._
_Last updated: 2026-10-31 (intraday conversion)_

## Core approach: liquid momentum, flat by the close

Trade the most liquid Nifty 500 names when volume confirms a move and the index agrees.
Take 1% to 2% intraday moves with 0.5% to 1.0% stops.
Everything is closed by 15:15 IST. Doing nothing is a valid outcome of every scan.

All numbers below are starting hypotheses.
They become rules only after 20 or more closed trades in the relevant bucket support them.

## Session map (IST)

| Time | Mode | Rule |
|------|------|------|
| 09:15 - 09:30 | Observe | The opening range forms. No entries |
| 09:30 - 10:30 | Prime | Best volume and trend. ORB and VWAP setups at normal allowed size |
| 10:30 - 13:00 | Selective | Midday chop. Take only `rvol` of 1.5 or more with `aligned: true` |
| 13:00 - 14:30 | Afternoon | Trend continuation and late breakouts. Last entry at 14:30 |
| 14:30 - 15:15 | Manage | No entries. Let stops and targets work |
| 15:15 | Flat | `portfolio.py monitor` squares off everything |

## Setups

### ORB (opening range break)

- Trigger: scan signal `ORB_LONG` or `ORB_SHORT`. A completed 15-minute bar closed beyond the 09:15 candle extreme and on the right side of VWAP.
- Confirm: `rvol` of at least 1.2 (prefer 1.5), `aligned: true`, and a gap in the same direction is a plus.
- Skip if `orb_range_pct` is above 1.0, because a stop at the other end of the range breaks the 1% limit.
- Skip if price is already more than 0.5% beyond the break level. The stop would be too far.
- Stop: the opposite side of the opening range, or VWAP, whichever is closer, within 0.5% to 1.0%.
- Target: 1.5 to 2 times the stop distance, typically 1% to 2%.

### VWAP reclaim and reject

- Trigger: scan signal `VWAP_RECLAIM` (long) or `VWAP_REJECT` (short) on the latest completed bar.
- Confirm: `rvol` of at least 1.2, `aligned: true`, and relative strength against the segment benchmark in the trade direction (`rs_vs_ref` positive for longs, negative for shorts).
- Skip a stock that has crossed VWAP several times today. That is chop, not a reclaim.
- Stop: beyond the signal bar's low (long) or high (short), within 0.5% to 1.0%.
- Target: the day's high or low, or 1.5 times the stop distance, whichever is nearer.

## Segments

The Nifty 500 is the Nifty 100 (`LARGE`), the Nifty Midcap 150 (`MID`) and the Nifty Smallcap 250 (`SMALL`).
Each name is judged against the backdrop of its own segment: Nifty 50 for `LARGE`, Midcap 150 for `MID` and `SMALL`.
`regime` shows both. When they disagree, trade only the segment whose backdrop matches the signal.

| Segment | Slippage per fill | Default MIS | Starting stance |
|---------|-------------------|-------------|-----------------|
| `LARGE` | 0.03% | 5x | Baseline |
| `MID` | 0.06% | 3x | Wider spreads. Prefer `rvol` of 1.5 or more and targets of 1.5% or more, so the net reward:risk gate passes |
| `SMALL` | 0.10% | 2x | Only the 30 most liquid make the shortlist. Treat as exceptions and require `rvol` of 2 or more |

Judge each segment on its own expectancy in R after 20 trades.
If `MID` or `SMALL` runs negative, drop their shortlist slots before touching the large-cap rules.

## Regime filter

| Condition | Action |
|-----------|--------|
| Backdrop above VWAP | Longs preferred. Shorts only with `rs_vs_ref` below -1 and `rvol` of 2 or more |
| Backdrop below VWAP | Shorts preferred. Longs only with `rs_vs_ref` above 1 and `rvol` of 2 or more |
| India VIX below 13 | Quiet tape. Ranges are small, so expect targets near 1% |
| India VIX 13 to 20 | Normal |
| India VIX above 20 | Noisy. Take only `rvol` of 2 or more, and consider skipping the day |
| RBI policy, Budget, index expiry, heavy results day | Cut to one or two trades, or skip. Flag it in the pre-session brief |

## Exit rules

| Trigger | Action | Automatic |
|---------|--------|-----------|
| Stop touched | Exit at the stop. A gap through it fills at the open | Yes |
| +1R reached | Move the stop to cost | Yes |
| Target touched | Exit at the target | Yes |
| 15:15 IST | Square off | Yes |
| Daily loss reaches -2% | Close everything and halt | Yes |
| Thesis broken before the stop | Tell the user and suggest an exit. Do not exit alone | No |

## Position management

- Quantity comes from `portfolio.py size`. Never override it.
- One position per symbol, three open at most, two per industry at most.
- Never widen a stop. Never average a loser.
- Re-entering a symbol that just stopped out needs a new signal and a fresh reason, not hope.

## Candidate rules under test (not active)

Move a rule into the sections above only after 20 or more trades support it.

- Time stop: exit if the trade has made less than 0.3% progress after 60 minutes.
- Partial exit of half the position at +1R, then trail the rest.
- No entries between 11:30 and 13:00.

## Lessons Learned

_Updated by daily and weekly reviews._

| Date | Lesson | Action Taken |
|------|--------|--------------|
| - | - | - |

## Strategy Changelog

| Date | Change | Reason |
|------|--------|--------|
| 2026-10-31 | Converted from a swing strategy to intraday MIS on the Nifty 500 | Starting framework |
| 2026-10-31 | Tagged Nifty 100 / Midcap 150 / Smallcap 250 segments, gave each its own shortlist slots, costs and backdrop | Top-150-by-turnover held only 40 of the Midcap 150 names |
