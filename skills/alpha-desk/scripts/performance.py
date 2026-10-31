#!/usr/bin/env python3
"""Intraday performance analytics and review loop for Alpha Desk (reads the closed trades in portfolio.json)."""

import argparse
import json
from datetime import timedelta

from market_data import get_history, now_ist
from portfolio import PORTFOLIO_FILE, load_json, lock, log_decision

MIN_SAMPLE = 20  # closed trades in a bucket before a rule change is justified


def stats(ts):
    if not ts:
        return {"trades": 0}
    net = [t["net"] for t in ts]
    wins, losses = [x for x in net if x > 0], [x for x in net if x < 0]
    gp, gl = sum(wins), -sum(losses)
    run = peak = dd = 0.0
    for x in net:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    gross, fees = sum(t["gross"] for t in ts), sum(t["fees"] for t in ts)
    rs = [t["r"] for t in ts if t["r"] is not None]
    avg_win, avg_loss = (gp / len(wins) if wins else 0), (gl / len(losses) if losses else 0)
    return {
        "trades": len(ts), "win_rate": round(len(wins) / len(ts) * 100, 1),
        "net_pnl": round(sum(net), 2), "gross_pnl": round(gross, 2), "fees": round(fees, 2),
        "fees_pct_of_gross": round(fees / gross * 100, 1) if gross > 0 else None,
        "avg_win": round(avg_win, 2), "avg_loss": round(-avg_loss, 2),
        "payoff": round(avg_win / avg_loss, 2) if avg_loss else None,
        "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None,
        "profit_factor": round(gp / gl, 2) if gl else None,
        "avg_hold_min": round(sum(t["hold_min"] for t in ts) / len(ts), 1),
        "max_drawdown": round(dd, 2),
    }


def by(ts, key):
    groups = {}
    for t in ts:
        groups.setdefault(key(t), []).append(t)
    out = {}
    for k, g in sorted(groups.items()):
        rs = [t["r"] for t in g if t["r"] is not None]
        out[k] = {"n": len(g), "win_rate": round(sum(t["net"] > 0 for t in g) / len(g) * 100, 1),
                  "net": round(sum(t["net"] for t in g), 2),
                  "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None}
    return out


def brief(t):
    return {k: t[k] for k in ("symbol", "side", "setup", "opened_at", "net", "r", "reason", "note")}


def half_hour(t):
    return f"{t['opened_at'][11:13]}:{'00' if int(t['opened_at'][14:16]) < 30 else '30'}"


def suggest(ts, s, groups):
    if not ts:
        return ["No closed trades in this period."]
    out = []
    if any(t["r"] is not None and t["r"] < -1.5 for t in ts):
        out.append("A loss exceeded 1.5R: check stop fills (gaps) and that every stop was honoured.")
    if s["fees_pct_of_gross"] and s["fees_pct_of_gross"] > 30:
        out.append(f"Costs are {s['fees_pct_of_gross']}% of gross P&L: take fewer, higher-quality trades.")
    if s["trades"] < MIN_SAMPLE:
        out.append(f"Sample is {s['trades']} closed trades (need {MIN_SAMPLE}): log observations, do not change rules yet.")
        return out
    if s["expectancy_r"] is not None and s["expectancy_r"] <= 0:
        out.append(f"Expectancy is {s['expectancy_r']}R over {s['trades']} trades: find the losing bucket below.")
    for label, g in groups.items():
        out += [f"{label} {k}: {v['expectancy_r']}R over {v['n']} trades. Restrict or drop it."
                for k, v in g.items() if v["n"] >= MIN_SAMPLE and v["expectancy_r"] is not None and v["expectancy_r"] < 0]
    return out or ["Within parameters. No rule changes."]


def nifty_return(since):
    """Nifty 50 close-to-close return since `since`, for context only (an intraday book is flat overnight)."""
    rows = get_history("^NSEI", "3mo", "1d").get("data", [])
    before = [r for r in rows if r["time"] < since.isoformat()]
    if not rows:
        return None
    base = (before[-1] if before else rows[0])["close"]
    return round((rows[-1]["close"] / base - 1) * 100, 2)


def review(kind, days):
    pf = load_json(PORTFOLIO_FILE, None)
    if not pf:
        return {"error": "No portfolio yet. Nothing to review."}
    today = now_ist().date()
    since = today - timedelta(days=days)
    ts = [t for t in pf["trades"] if t["closed_at"][:10] >= since.isoformat()]
    s = stats(ts)
    groups = {"setup": by(ts, lambda t: t["setup"] or "none"), "side": by(ts, lambda t: t["side"]),
              "segment": by(ts, lambda t: t["segment"]), "entry_time": by(ts, half_hour),
              "sector": by(ts, lambda t: t["sector"] or "none")}
    out = {"type": kind, "date": today.isoformat(), "from": since.isoformat(), **s,
           **{f"by_{k}": v for k, v in groups.items()}, "by_exit_reason": by(ts, lambda t: t["reason"]),
           "suggestions": suggest(ts, s, groups)}
    if ts:
        out["best_trade"], out["worst_trade"] = brief(max(ts, key=lambda t: t["net"])), brief(min(ts, key=lambda t: t["net"]))
    if pf["day"]["date"] == today.isoformat():
        out["halted_today"] = pf["day"]["halted"]
    if kind == "monthly_review":
        try:
            out["nifty_return_pct"] = nifty_return(since)
        except Exception:
            out["nifty_return_pct"] = None
    with lock():
        log_decision(out)
    return out


def summary():
    pf = load_json(PORTFOLIO_FILE, None)
    if not pf:
        return {"error": "No portfolio found. It is created by the first trade."}
    return {"portfolio_name": pf["portfolio_name"], "initial_capital": pf["initial_capital"],
            "equity": round(pf["cash"], 2), "return_pct": round((pf["cash"] / pf["initial_capital"] - 1) * 100, 2),
            "open_positions": len(pf["positions"]), **stats(pf["trades"]),
            "by_setup": by(pf["trades"], lambda t: t["setup"] or "none")}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cmd", choices=["summary", "daily-review", "weekly-review", "monthly-review"])
    a = p.parse_args()
    result = {"summary": summary,
              "daily-review": lambda: review("daily_review", 0),
              "weekly-review": lambda: review("weekly_review", 6),
              "monthly-review": lambda: review("monthly_review", 29)}[a.cmd]()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
