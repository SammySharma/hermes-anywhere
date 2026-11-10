#!/usr/bin/env python3
"""Backtest the Alpha Desk setup on historical intraday bars, with the same rules, sizing and costs as the live desk.

  python backtest.py fetch --source dhan --days 1250 --interval 5 --out bars/     # needs DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID
  python backtest.py fetch --source yfinance --interval 5 --out bars/            # last 60 days only
  python backtest.py run --data bars/                                            # the v3 rules
  python backtest.py run --data bars/ --target-r 1.8 --breakeven --set stop_atr=0.25   # the old exit style, to compare
  python backtest.py run --data bars/ --sweep                                    # is the result a plateau or a spike?

Data: one CSV per symbol in the folder (SYMBOL.csv: time,open,high,low,close,volume; IST; 1, 5 or 15-minute bars).
Use 1 or 5-minute bars. With 15-minute bars a trigger and a stop inside one bar are counted as a loss, which is harsh.

The report splits results by market regime (bull, bear, neutral), by side, segment, year and by the first and second
half of the sample. The desk should not trade real money on a setup that fails the verdict at the bottom.
"""

import argparse
import glob
import json
import math
import os
import sys
import time as _time
import urllib.request
from datetime import datetime, timedelta

import portfolio as pf
import strategy
from market_data import IST, MIN_ATR_PCT, MIN_PRICE, MIN_TURNOVER_CR, SHORTLIST_CAPS, load_universe, shortable

SQUARE_OFF_MIN = 15 * 60 + 15


def _mins(t):
    return t.hour * 60 + t.minute


# --- data -------------------------------------------------------------------------------------------------------------

def load(folder, start=None, end=None):
    """{symbol: {"days": {date: arrays}, "daily": DataFrame}} from the CSVs in `folder`."""
    import pandas as pd

    out = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.csv"))):
        d = pd.read_csv(path)
        d.columns = [c.strip().lower() for c in d.columns]
        d["time"] = pd.to_datetime(d["time"])
        d = d.set_index("time").sort_index()
        d.index = d.index.tz_localize(None) if d.index.tz is None else d.index.tz_convert(IST).tz_localize(None)
        if start:
            d = d[d.index >= start]
        if end:
            d = d[d.index < end]
        d = d[(d.index.time >= strategy.OPEN) & (d["volume"] >= 0)].dropna(subset=["close"])
        if d.empty:
            continue
        out[os.path.basename(path)[:-4].upper()] = d
    return out


def prepare(raw, p):
    """Per symbol: the bars of each session as arrays, and a daily table whose ATR, turnover and previous close use
    completed sessions only (shifted by one day), so nothing in a day's plan comes from that day or later."""
    import pandas as pd

    end = _mins(strategy.or_end(p))
    out = {}
    for sym, d in raw.items():
        days, rows = {}, []
        for day, g in d.groupby(d.index.date):
            m = (g.index.hour * 60 + g.index.minute).to_numpy()
            o, h, l, c, v = (g[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
            if m[0] != _mins(strategy.OPEN):
                continue  # a session without its 09:15 bar has no opening range
            k = int((m < end).sum())
            days[day] = (m, o, h, l, c, v, k)
            rows.append((day, o[0], h.max(), l.min(), c[-1], v.sum(), o[0], h[:k].max(), l[:k].min(), c[k - 1], v[:k].sum()))
        if len(rows) < 20:
            continue
        t = pd.DataFrame(rows, columns=["day", "open", "high", "low", "close", "volume", "oo", "oh", "ol", "oc", "ov"]).set_index("day")
        pc = t["close"].shift()
        tr = pd.concat([t["high"] - t["low"], (t["high"] - pc).abs(), (t["low"] - pc).abs()], axis=1).max(axis=1)
        t["prev_close"] = pc
        t["atr"] = tr.rolling(14).mean().shift()
        t["turnover_cr"] = (t["close"] * t["volume"]).rolling(20, min_periods=15).mean().shift() / 1e7
        t["ret"] = t["close"].pct_change()
        out[sym] = {"days": days, "daily": t}
    return out


def regimes(data):
    """{date: bull | bear | neutral | unknown} from an equal-weight index of the loaded stocks, known before the open.
    bull: above its 50-day average and up more than 2% in 20 days. bear: the mirror image. neutral: everything else."""
    import pandas as pd

    rets = pd.concat([x["daily"]["ret"] for x in data.values()], axis=1).mean(axis=1).fillna(0)
    lvl = (1 + rets).cumprod()
    sma, r20 = lvl.rolling(50, min_periods=30).mean(), lvl.pct_change(20) * 100
    lab = pd.Series("neutral", index=lvl.index)
    lab[(lvl > sma) & (r20 > 2)] = "bull"
    lab[(lvl < sma) & (r20 < -2)] = "bear"
    lab[sma.isna() | r20.isna()] = "unknown"
    return lab.shift().fillna("unknown").to_dict()


# --- simulation -------------------------------------------------------------------------------------------------------

def simulate(data, p, target_r=None, breakeven=False, capital=pf.CAPITAL):
    uni = load_universe()
    reg = regimes(data)
    all_days = sorted({d for x in data.values() for d in x["days"]})
    until = _mins(strategy.until(p))
    equity, trades = float(capital), []
    for day in all_days:
        cands = []
        for sym, x in data.items():
            if day not in x["days"] or day not in x["daily"].index:
                continue
            t = x["daily"].loc[day]
            seg = uni.get(sym, {}).get("segment", "SMALL")
            if not SHORTLIST_CAPS[seg] or not t["atr"] == t["atr"] or not t["turnover_cr"] == t["turnover_cr"]:
                continue
            if t["prev_close"] < MIN_PRICE or t["turnover_cr"] < MIN_TURNOVER_CR or t["atr"] / t["prev_close"] * 100 < MIN_ATR_PCT:
                continue
            hist = x["daily"]["ov"]
            prior = hist[hist.index < day].tail(p["rvol_days"]).tolist()
            plan, _ = strategy.opening_plan(t["oo"], t["oh"], t["ol"], t["oc"], t["ov"], prior, t["atr"], p)
            if plan and (plan["side"] == "LONG" or shortable(sym)):
                cands.append({"symbol": sym, "segment": seg, **plan})
        cands = sorted(cands, key=lambda c: -c["or_rvol"])[:p["top"]]
        if not cands:
            continue

        start_eq, opened, halted, pending, open_ = equity, 0, False, list(cands), []
        bars = {c["symbol"]: data[c["symbol"]]["days"][day] for c in cands}
        pos_of = {s: {int(m): i for i, m in enumerate(b[0])} for s, b in bars.items()}
        clock = sorted({int(m) for b in bars.values() for m in b[0][b[6]:]})

        def close(pos, px, reason, minute):
            nonlocal equity
            sign = 1 if pos["side"] == "LONG" else -1
            _, x_side = pf.sides(pos["side"])
            out = pf.fill(x_side, px, pos["segment"])
            net = (out - pos["entry"]) * pos["qty"] * sign - pos["fees_in"] - pf.charges(x_side, pos["qty"] * out)
            equity += net
            open_.remove(pos)
            trades.append({"day": day, "symbol": pos["symbol"], "side": pos["side"], "segment": pos["segment"],
                           "regime": reg.get(day, "unknown"), "net": net, "r": net / pos["risk"], "reason": reason,
                           "cost_r": pos["cost_r"], "or_rvol": pos["or_rvol"], "hold_min": minute - pos["minute"],
                           "equity": equity})

        for minute in clock:
            if minute >= SQUARE_OFF_MIN:
                break
            for pos in list(open_):  # positions from earlier bars: stop first, then target
                i = pos_of[pos["symbol"]].get(minute)
                if i is None:
                    continue
                _, o, h, l, c, _, _ = bars[pos["symbol"]]
                sgn = 1 if pos["side"] == "LONG" else -1
                adverse, favorable = (l[i], h[i]) if sgn > 0 else (h[i], l[i])
                if (adverse - pos["stop"]) * sgn <= 0:
                    close(pos, pos["stop"] if (o[i] - pos["stop"]) * sgn > 0 else o[i],
                          "stop" if pos["stop"] == pos["stop0"] else "breakeven", minute)
                elif pos["target"] is not None and (favorable - pos["target"]) * sgn >= 0:
                    close(pos, pos["target"], "target", minute)
                elif breakeven and pos["stop"] == pos["stop0"] and (favorable - pos["entry"]) * sgn >= abs(pos["entry"] - pos["stop0"]):
                    pos["stop"] = pos["entry"]
            if not halted and equity - start_eq <= -pf.DAILY_LOSS_PCT / 100 * start_eq:
                halted, pending = True, []
                for pos in list(open_):
                    i = pos_of[pos["symbol"]].get(minute)
                    b = bars[pos["symbol"]]
                    close(pos, b[4][i] if i is not None else b[4][max(j for m, j in pos_of[pos["symbol"]].items() if m <= minute)],
                          "daily-loss-halt", minute)
            if minute >= until:
                pending = []
            for c in list(pending):  # armed stop-entries
                i = pos_of[c["symbol"]].get(minute)
                if i is None:
                    continue
                _, o, h, l, cl, _, _ = bars[c["symbol"]]
                sgn = 1 if c["side"] == "LONG" else -1
                if ((h[i] if sgn > 0 else l[i]) - c["trigger"]) * sgn < 0:
                    continue
                pending.remove(c)
                if halted or opened >= pf.MAX_TRADES_PER_DAY or len(open_) >= pf.MAX_POSITIONS:
                    continue
                px = o[i] if (o[i] - c["trigger"]) * sgn > 0 else c["trigger"]
                seg = c["segment"]
                used = sum(q["qty"] * q["entry"] / pf.SEGMENTS[q["segment"]]["mult"] for q in open_)
                pl = pf.plan(c["side"], px, c["stop"], None, equity, used, pf.SEGMENTS[seg]["mult"], seg)
                if pl["problems"]:
                    continue
                e_side, _ = pf.sides(c["side"])
                dist = abs(pl["entry"] - c["stop"])
                pos = {"symbol": c["symbol"], "side": c["side"], "segment": seg, "qty": pl["qty"], "entry": pl["entry"],
                       "stop": c["stop"], "stop0": c["stop"], "risk": pl["risk"], "cost_r": pl["cost_r"],
                       "target": pl["entry"] + sgn * target_r * dist if target_r else None,
                       "fees_in": pf.charges(e_side, pl["qty"] * pl["entry"]), "or_rvol": c["or_rvol"], "minute": minute}
                open_.append(pos)
                opened += 1
                if ((l[i] if sgn > 0 else h[i]) - c["stop"]) * sgn <= 0:  # the trigger bar also reached the stop
                    close(pos, c["stop"], "stop", minute)
        for pos in list(open_):  # square-off: the open of the 15:15 bar, else the last price before it
            b, idx = bars[pos["symbol"]], pos_of[pos["symbol"]]
            later = [j for m, j in idx.items() if m >= SQUARE_OFF_MIN]
            px = b[1][min(later)] if later else b[4][-1]
            close(pos, px, "square-off", SQUARE_OFF_MIN)
    return trades


# --- report -----------------------------------------------------------------------------------------------------------

def stats(ts, capital=pf.CAPITAL):
    if not ts:
        return {"n": 0}
    rs = [t["r"] for t in ts]
    n, mean = len(rs), sum(rs) / len(rs)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    wins, losses = sum(t["net"] for t in ts if t["net"] > 0), -sum(t["net"] for t in ts if t["net"] < 0)
    return {"n": n, "win_pct": round(sum(r > 0 for r in rs) / n * 100, 1), "exp_r": round(mean, 3),
            "t": round(mean / sd * math.sqrt(n), 2) if sd else None,
            "pf": round(wins / losses, 2) if losses else None, "net": round(sum(t["net"] for t in ts)),
            "cost_r": round(sum(t["cost_r"] for t in ts) / n, 2)}


def report(ts, capital=pf.CAPITAL):
    if not ts:
        return {"error": "No trades. Check the data folder, the date range and the filters."}
    peak = dd = 0.0
    for t in ts:
        peak = max(peak, t["equity"])
        dd = max(dd, (peak - t["equity"]) / peak * 100)
    days = sorted({t["day"] for t in ts})
    mid = days[len(days) // 2]

    def by(key):
        groups = {}
        for t in ts:
            groups.setdefault(key(t), []).append(t)
        return {str(k): stats(g) for k, g in sorted(groups.items(), key=lambda kv: str(kv[0]))}

    out = {"from": str(days[0]), "to": str(days[-1]), "trading_days": len(days), **stats(ts),
           "return_pct": round((ts[-1]["equity"] / capital - 1) * 100, 1), "max_drawdown_pct": round(dd, 1),
           "by_regime": by(lambda t: t["regime"]), "by_side": by(lambda t: t["side"]),
           "by_segment": by(lambda t: t["segment"]), "by_year": by(lambda t: t["day"].year),
           "by_exit": by(lambda t: t["reason"]),
           "by_half": by(lambda t: "first_half" if t["day"] < mid else "second_half")}
    out["verdict"] = verdict(out)
    return out


def verdict(r):
    """Plain pass/fail. A setup that has not passed this on NSE data is a hypothesis, whatever it did in a US paper."""
    fails = []
    if r["n"] < 200:
        fails.append(f"only {r['n']} trades (need 200): the result is noise either way")
    if r["exp_r"] <= 0:
        fails.append(f"expectancy is {r['exp_r']}R after costs")
    elif (r["t"] or 0) < 2:
        fails.append(f"t-stat {r['t']} is below 2: indistinguishable from luck")
    for name in ("bull", "bear", "neutral"):
        g = r["by_regime"].get(name)
        if not g or g["n"] < 30:
            fails.append(f"{name} regime has {g['n'] if g else 0} trades (need 30): untested there")
        elif g["exp_r"] <= 0:
            fails.append(f"loses in the {name} regime ({g['exp_r']}R over {g['n']} trades)")
    for name, g in r["by_half"].items():
        if g["exp_r"] <= 0:
            fails.append(f"negative in the {name.replace('_', ' ')} ({g['exp_r']}R): not stable over time")
    return {"pass": not fails, "reasons": fails or ["Positive after costs in every regime and in both halves."]}


# --- fetch ------------------------------------------------------------------------------------------------------------

def _symbols(arg):
    if arg:
        return [s.strip().upper() for s in arg.split(",")]
    return [s for s, v in load_universe(refresh=True).items() if SHORTLIST_CAPS[v["segment"]]]


def fetch_yfinance(symbols, interval, out):
    from market_data import _download
    got = _download([s + ".NS" for s in symbols], "60d", f"{interval}m")
    for y, d in got.items():
        d = d.tz_localize(None) if d.index.tz is not None else d
        d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        d.index.name = "time"
        d.to_csv(os.path.join(out, y[:-3] + ".csv"))
    return len(got)


def fetch_dhan(symbols, interval, days, out):
    """DhanHQ v2 intraday history: up to 5 years, 90 days per request (needs the Data API subscription)."""
    import csv
    import io

    token, client = os.environ.get("DHAN_ACCESS_TOKEN"), os.environ.get("DHAN_CLIENT_ID")
    if not token or not client:
        sys.exit("Set DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID.")
    raw = urllib.request.urlopen("https://images.dhan.co/api-data/api-scrip-master.csv", timeout=60).read().decode("utf-8", "replace")
    ids = {r["SEM_TRADING_SYMBOL"]: r["SEM_SMST_SECURITY_ID"] for r in csv.DictReader(io.StringIO(raw))
           if r.get("SEM_EXM_EXCH_ID") == "NSE" and r.get("SEM_SEGMENT") == "E" and r.get("SEM_SERIES") == "EQ"}
    today, done = datetime.now(IST).date(), 0
    for sym in symbols:
        if sym not in ids:
            print(f"{sym}: not in Dhan's scrip master, skipped", file=sys.stderr)
            continue
        rows, end = [], today + timedelta(days=1)
        while (today - end).days < days:
            start = end - timedelta(days=89)
            body = {"securityId": ids[sym], "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "interval": str(interval),
                    "oi": False, "fromDate": f"{start} 09:15:00", "toDate": f"{end} 15:30:00"}
            req = urllib.request.Request("https://api.dhan.co/v2/charts/intraday", json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "Accept": "application/json",
                                          "access-token": token, "client-id": client})
            try:
                r = json.loads(urllib.request.urlopen(req, timeout=30).read())
            except Exception as e:
                print(f"{sym} {start}: {e}", file=sys.stderr)
                break
            ts = r.get("timestamp") or []
            if not ts:
                break
            rows += [(datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M:%S"), r["open"][i], r["high"][i], r["low"][i],
                      r["close"][i], r["volume"][i]) for i, t in enumerate(ts)]
            end = start
            _time.sleep(0.25)
        if rows:
            with open(os.path.join(out, sym + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "open", "high", "low", "close", "volume"])
                w.writerows(sorted(set(rows)))
            done += 1
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--source", choices=["yfinance", "dhan"], required=True)
    f.add_argument("--interval", type=int, default=5, choices=[1, 5, 15])
    f.add_argument("--days", type=int, default=1250, help="calendar days of history (dhan only)")
    f.add_argument("--symbols", help="comma separated; default is the Nifty 100 and Midcap 150")
    f.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data", required=True)
    r.add_argument("--from", dest="start")
    r.add_argument("--to", dest="end")
    r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a strategy.PARAMS value")
    r.add_argument("--target-r", type=float, help="exit at this multiple of the stop distance (default: no target)")
    r.add_argument("--breakeven", action="store_true", help="move the stop to cost at +1R (the old rule)")
    r.add_argument("--sweep", action="store_true", help="grid over stop_atr and min_or_rvol")
    r.add_argument("--trades", help="write every trade to this CSV")
    a = ap.parse_args()

    if a.cmd == "fetch":
        os.makedirs(a.out, exist_ok=True)
        syms = _symbols(a.symbols)
        n = fetch_yfinance(syms, a.interval, a.out) if a.source == "yfinance" else fetch_dhan(syms, a.interval, a.days, a.out)
        print(json.dumps({"symbols_saved": n, "folder": a.out}))
        return

    p = dict(strategy.PARAMS)
    for kv in a.set:
        k, v = kv.split("=", 1)
        if k not in p:
            sys.exit(f"Unknown parameter {k}. Known: {', '.join(p)}")
        p[k] = type(p[k])(v)
    data = prepare(load(a.data, a.start, a.end), p)
    if not data:
        sys.exit("No usable CSVs (need 20 or more sessions per symbol).")
    if a.sweep:
        grid = []
        for stop_atr in (0.25, 0.4, 0.6, 0.8):
            for rvol in (1.5, 2.0, 3.0):
                s = stats(simulate(data, {**p, "stop_atr": stop_atr, "min_or_rvol": rvol}, a.target_r, a.breakeven))
                grid.append({"stop_atr": stop_atr, "min_or_rvol": rvol, **s})
        print(json.dumps({"note": "Trust a broad plateau of positive cells. One good cell among bad ones is curve fitting.",
                          "grid": grid}, indent=1))
        return
    ts = simulate(data, p, a.target_r, a.breakeven)
    if a.trades and ts:
        import csv
        with open(a.trades, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(ts[0]))
            w.writeheader()
            w.writerows(ts)
    print(json.dumps({"params": p, "exit": {"target_r": a.target_r, "breakeven": a.breakeven}, **report(ts)}, indent=1))


if __name__ == "__main__":
    main()
