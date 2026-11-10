#!/usr/bin/env python3
"""Intraday market data for the Nifty 500 MIS desk.

nse-live (NSE's own MCP, 1-3 minutes delayed) is the primary source for session status, quotes, VWAP and the index
backdrop. Yahoo Finance supplies the intraday bars NSE does not publish, and is the fallback when nse-live is down.
"""

import argparse
import csv
import gzip
import json
import logging
import os
import subprocess
import sys
import time as _time
import urllib.error
import urllib.request
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone

import strategy

IST = timezone(timedelta(hours=5, minutes=30))
OPEN, ENTRY_START, LAST_ENTRY, SQUARE_OFF, CLOSE = time(9, 15), time(9, 30), time(14, 30), time(15, 15), time(15, 30)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The paper book lives under the Hermes data folder, not in the skill folder, so updating or reinstalling the skill
# never touches it (in the Docker image HERMES_HOME is /opt/data, the mounted hermes-data volume).
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
DATA_DIR = os.environ.get("ALPHA_DESK_DATA") or os.path.join(HERMES_HOME, "alpha-desk-data")

NSE_MCP_URL = os.environ.get("NSE_MCP_URL", "https://mcp.nseindia.in/cmmkt/mcp")
NSE_MAX_AGE_S = 360  # NSE says the feed runs 1-3 minutes behind; an older quote is stale
LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_{}list.csv"
LISTS = {"nifty500": 400, "nifty100": 90, "niftymidcap150": 140}  # index list -> minimum rows of a valid download

# Shortlist filters (daily data, last 20 sessions) and per-segment slots, so midcaps are not crowded out by large caps.
# Tune from the weekly review.
MIN_PRICE, MIN_TURNOVER_CR, MIN_ATR_PCT = 50, 50, 1.0
# Smallcaps get no slots: about 0.3% of round-trip cost is a third of a 1% stop. Re-open them only if backtest.py
# shows the SMALL segment paying for itself.
SHORTLIST_CAPS = {"LARGE": 80, "MID": 100, "SMALL": 0}
FNO_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
STALE_BAR_MIN = 18  # a live 15m bar older than this means the Yahoo feed is lagging
NIFTY_ETF, MID_ETF = "NIFTYBEES", "MID150BEES"  # indices have no volume, so their ETFs supply the VWAP side
REF = {"LARGE": "nifty", "MID": "midcap150", "SMALL": "midcap150"}  # which backdrop each segment is judged against


def now_ist():
    """Current IST time. ALPHA_DESK_NOW=YYYY-MM-DDTHH:MM replays a past session (tests, post-mortems)."""
    fake = os.environ.get("ALPHA_DESK_NOW")
    return datetime.fromisoformat(fake).replace(tzinfo=IST) if fake else datetime.now(IST)


def _use_nse():
    """nse-live only has the present, so replays and ALPHA_DESK_SOURCE=yfinance skip it."""
    return not os.environ.get("ALPHA_DESK_NOW") and os.environ.get("ALPHA_DESK_SOURCE", "nse-live") != "yfinance"


def _yf():
    try:
        import yfinance
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "yfinance"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import yfinance
    warnings.filterwarnings("ignore")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    return yfinance


def _sym(symbol):
    s = symbol.upper()
    return s if s.startswith("^") else s.removesuffix(".NS")


def _y(symbol):
    s = symbol.upper()
    return s if s.startswith("^") or s.endswith((".NS", ".BO")) else s + ".NS"


def _r(x, nd=2):
    return None if x is None or x != x else round(float(x), nd)


def _load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


# --- Yahoo Finance: intraday bars and fallback quotes --------------------------------------------------------------

def _download(symbols, period, interval):
    """{yahoo_symbol: OHLCV frame}. Intraday frames are IST-indexed; symbols with no data are omitted."""
    yf = _yf()
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        try:
            df = yf.download(chunk, period=period, interval=interval, group_by="ticker", progress=False)
        except Exception:
            continue
        for s in chunk:
            try:
                d = df[s] if df.columns.nlevels > 1 else df
            except KeyError:
                continue
            d = d.dropna(subset=["Close"])
            if d.empty:
                continue
            out[s] = d.tz_convert(IST) if d.index.tz is not None else d
    return out


def _vwap(d):
    tp = (d["High"] + d["Low"] + d["Close"]) / 3
    return (tp * d["Volume"]).cumsum() / d["Volume"].cumsum()


def get_bars(symbols, period, interval, now=None):
    """{symbol: intraday bars that started at or before `now`}. Symbols may omit .NS."""
    now = now or now_ist()
    got = _download([_y(s) for s in symbols], period, interval)
    return {s: got[_y(s)][got[_y(s)].index <= now] for s in symbols if _y(s) in got}


def _yahoo_quotes(symbols, now):
    """{symbol: quote} from 1-minute bars. Session VWAP is typical-price weighted."""
    bars = get_bars(symbols, "1d", "1m", now)
    daily = _download([_y(s) for s in symbols], "5d", "1d")
    out = {}
    for s, d in bars.items():
        if d.empty:
            continue
        sess = d.index[-1].date()
        dd = daily.get(_y(s))
        before = dd[dd.index.date < sess] if dd is not None else None
        prev = float(before["Close"].iloc[-1]) if before is not None and not before.empty else None
        ltp, age = float(d["Close"].iloc[-1]), (now - d.index[-1]).total_seconds()
        out[s] = {
            "symbol": s, "ltp": _r(ltp), "open": _r(d["Open"].iloc[0]), "high": _r(d["High"].max()),
            "low": _r(d["Low"].min()), "prev_close": _r(prev),
            "chg_pct": _r((ltp / prev - 1) * 100) if prev else None,
            "vwap": _r(_vwap(d).iloc[-1]), "volume": int(d["Volume"].sum()),
            "last_bar": d.index[-1].strftime("%H:%M"), "age_s": int(age), "session": str(sess),
            "live": sess == now.date() and age <= 240, "src": "yfinance",
        }
    return out


# --- nse-live: NSE's own MCP server ---------------------------------------------------------------------------------

def _mcp_post(payload, sid=None):
    """One JSON-RPC POST to the NSE MCP. Returns (http status, session id, content type, body text)."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "User-Agent": "alpha-desk/2"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(NSE_MCP_URL, json.dumps(payload).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, rsid, ctype, raw = r.status, r.headers.get("Mcp-Session-Id"), r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        status, rsid, ctype, raw = e.code, None, "", e.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return status, rsid, ctype, raw.decode("utf-8", "replace")


def _rpc_message(body, ctype, want_id):
    if "text/event-stream" in ctype:
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    m = json.loads(line[5:])
                except ValueError:
                    continue
                if m.get("id") == want_id:
                    return m
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _mcp(tool, args):
    """Call an nse-live tool and return its JSON payload, or None on any failure (callers fall back to Yahoo).
    NSE's MCP has no sticky sessions: about half of the follow-up requests reach an instance that never saw the
    session and get a 404. Re-initialising and retrying works, and notifications/initialized is not required."""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "alpha-desk", "version": "2"}}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    deadline, down = _time.time() + 25, 0
    for _ in range(12):
        if _time.time() > deadline or down >= 3:
            break
        try:
            status, sid, _, _ = _mcp_post(init)
            if status != 200 or not sid:
                continue
            status, _, ctype, body = _mcp_post(call, sid)
        except OSError:  # unreachable, timed out or reset
            down += 1
            continue
        if status == 404:
            continue
        if status != 200:
            return None
        try:
            res = _rpc_message(body, ctype, 2)["result"]
            data = None if res.get("isError") else json.loads(res["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return None if isinstance(data, dict) and data.get("error") else data
    return None


def _mcp_many(calls, ttl=30):
    """[(tool, args), ...] -> [JSON or None]. Served from a short disk cache, fetched in parallel.
    The cache lets the status, monitor, scan and price commands of one scan turn share quotes."""
    if not _use_nse():
        return [None] * len(calls)
    path = os.path.join(DATA_DIR, "nse_cache.json")
    cache, now = _load(path) or {}, _time.time()
    keys = [f"{t}|{json.dumps(a, sort_keys=True)}" for t, a in calls]
    todo = sorted({k: i for i, k in enumerate(keys) if now - cache.get(k, {}).get("t", 0) > ttl}.values())
    if todo:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for i, v in zip(todo, ex.map(lambda i: _mcp(*calls[i]), todo)):
                if v is not None:
                    cache[keys[i]] = {"t": now, "v": v}
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({k: e for k, e in cache.items() if now - e["t"] < 300}, f)
        os.replace(tmp, path)
    return [cache[k]["v"] if k in cache and now - cache[k]["t"] <= ttl else None for k in keys]


_MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def _parse_nse_stamp(s):
    """'30-Oct-2026 15:30:00' -> IST datetime, without depending on the locale."""
    try:
        day, time_part = s.split(" ")
        d, mon, y = day.split("-")
        h, m, sec = (time_part.split(":") + ["0"])[:3]
        return datetime(int(y), _MONTHS[mon.upper()], int(d), int(h), int(m), int(sec), tzinfo=IST)
    except (KeyError, ValueError):
        return None


def _norm_quote(r, now):
    """cm_get_stock_quote payload -> quote dict. VWAP is traded value (rupees crore) over volume."""
    s = (r or {}).get("stock") or {}
    if not s.get("lastTradedPrice"):
        return None
    try:
        stamp = datetime.strptime(s["latestTimestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except (KeyError, ValueError):
        return None
    vol, lo, hi = s.get("volume") or 0, s.get("lowPrice") or 0, s.get("highPrice") or 0
    vwap = (s.get("value") or 0) * 1e7 / vol if vol else None
    if vwap and hi and not lo * 0.995 <= vwap <= hi * 1.005:
        vwap = None  # outside the day's range: a unit surprise must not poison the signal
    age = max(0.0, (now - stamp).total_seconds())
    return {
        "symbol": s["symbol"], "ltp": _r(s["lastTradedPrice"]), "open": _r(s.get("openPrice")), "high": _r(hi),
        "low": _r(lo), "prev_close": _r(s.get("preClosePrice")), "chg_pct": _r(s.get("perChange")),
        "vwap": _r(vwap), "volume": int(vol), "last_bar": stamp.strftime("%H:%M"), "age_s": int(age),
        "session": str(stamp.date()), "live": stamp.date() == now.date() and age <= NSE_MAX_AGE_S, "src": "nse-live",
    }


def nse_quotes(symbols, now=None):
    """One quote per symbol from nse-live (None where it has none)."""
    now = now or now_ist()
    return [_norm_quote(r, now) for r in _mcp_many([("cm_get_stock_quote", {"symbol": s}) for s in symbols])]


def nse_indices(names):
    """Latest published value of NSE indices (exact live names such as 'NIFTY 50', 'NIFTY MIDCAP 150', 'INDIA VIX')."""
    out = []
    for r in _mcp_many([("cm_get_index_quote", {"indexName": n}) for n in names]):
        i, stamp = (r or {}).get("index"), _parse_nse_stamp((r or {}).get("nseTimestamp", ""))
        out.append({"name": i["indexName"], "last": i["last"], "prev_close": i["previousClose"],
                    "chg_pct": i["percChange"], "stamp": stamp} if i and stamp else None)
    return out


def nse_index(name):
    return nse_indices([name])[0]


def get_quotes(symbols):
    """One quote per symbol with session VWAP. nse-live first; Yahoo 1-minute bars when nse-live is down, stale or
    has no quote. `src` says which source answered and `live` whether the quote is fresh."""
    now = now_ist()
    symbols = [_sym(s) for s in symbols]
    plain = [s for s in symbols if not s.startswith("^")]
    got = dict(zip(plain, nse_quotes(plain, now)))
    out = {s: q for s, q in got.items() if q and q["live"]}
    rest = [s for s in symbols if s not in out]
    yq = _yahoo_quotes(rest, now) if rest else {}
    for s in rest:
        y = yq.get(s)
        out[s] = y if y and y["live"] else (got.get(s) or y or {"symbol": s, "error": "No data"})
    return [out[s] for s in symbols]


# --- universe ---------------------------------------------------------------------------------------------------------

def _constituents(name, refresh):
    """Rows of one NSE index list. Refreshed weekly from NSE, falling back to the cache, then to the seed in
    references/ (NSE blocks some cloud IPs)."""
    cache = os.path.join(DATA_DIR, name + ".csv")
    if refresh and (not os.path.exists(cache) or _time.time() - os.path.getmtime(cache) > 7 * 86400):
        try:
            req = urllib.request.Request(LIST_URL.format(name), headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if raw.count(b"\n") < LISTS[name] or b"Symbol" not in raw[:300]:
                raise ValueError("unexpected NSE response")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(cache, "wb") as f:
                f.write(raw)
        except Exception:
            pass
    path = cache if os.path.exists(cache) else os.path.join(SKILL_DIR, "references", name + ".csv")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_universe(refresh=False):
    """Nifty 500 EQ-series constituents as {symbol: {"industry", "segment"}}.
    Nifty 500 is Nifty 100 (LARGE) + Midcap 150 (MID) + Smallcap 250 (SMALL), so segments come from the first two lists.
    Non-EQ series (BE trade-to-trade, rights) cannot be squared off intraday and are dropped."""
    large = {r["Symbol"] for r in _constituents("nifty100", refresh)}
    mid = {r["Symbol"] for r in _constituents("niftymidcap150", refresh)}
    return {r["Symbol"]: {"industry": r["Industry"],
                          "segment": "LARGE" if r["Symbol"] in large else "MID" if r["Symbol"] in mid else "SMALL"}
            for r in _constituents("nifty500", refresh) if r["Series"] == "EQ"}


def fno_symbols(refresh=False):
    """Nifty 500 names with stock futures, from NSE's lot-size file (cached weekly), or None if it is unavailable."""
    cache = os.path.join(DATA_DIR, "fno_mktlots.csv")
    if refresh and (not os.path.exists(cache) or _time.time() - os.path.getmtime(cache) > 7 * 86400):
        try:
            req = urllib.request.Request(FNO_URL, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if raw.count(b"\n") < 100:
                raise ValueError("unexpected NSE response")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(cache, "wb") as f:
                f.write(raw)
        except Exception:
            pass
    if not os.path.exists(cache):
        return None
    uni = load_universe()
    with open(cache, newline="", encoding="utf-8-sig", errors="replace") as f:
        found = {r[1].strip().upper() for r in csv.reader(f) if len(r) > 1} & set(uni)
    return found if len(found) >= 100 else None


def shortable(symbol):
    """Shorts only in F&O stocks: they have no fixed price band, so a short cannot be locked in at an upper circuit and
    sent to auction. Without NSE's F&O list the Nifty 100 stands in for it."""
    fno, sym = fno_symbols(), _sym(symbol)
    return sym in fno if fno else segment_of(sym) == "LARGE"


def segment_of(symbol):
    """LARGE / MID / SMALL. A stock outside the Nifty 500 is treated as SMALL, the most expensive to trade."""
    return load_universe().get(_sym(symbol), {}).get("segment", "SMALL")


# --- status, screen, regime, scan -------------------------------------------------------------------------------------

def status():
    """Session state from the latest published Nifty 50 value (nse-live, else Yahoo bars), so holidays and special
    sessions need no calendar upkeep."""
    now = now_ist()
    t = now.time()
    src = None
    idx = nse_index("NIFTY 50")
    if idx and idx["stamp"].date() == now.date():
        src = "nse-live"
    else:
        bars = _download(["^NSEI"], "1d", "1m").get("^NSEI")
        if bars is not None and bars.index[-1].date() == now.date():
            src = "yfinance"
    if src:
        state = "pre_open" if t < OPEN else "open" if t < CLOSE else "closed"
    elif now.weekday() >= 5 or t >= time(9, 20):
        state = "holiday"  # no value for today by 09:20 on a weekday: holiday or feed outage, either way do not trade
    else:
        state = "pre_open"
    res = {
        "state": state,
        "time_ist": now.strftime("%Y-%m-%d %H:%M"),
        "entry_window": state == "open" and ENTRY_START <= t <= LAST_ENTRY,
        "square_off_due": t >= SQUARE_OFF,
        "src": src or "none",
    }
    if state == "holiday":
        res["error"] = "Market Holiday"
    return res


def screen():
    """Daily liquidity/volatility screen of the Nifty 500 -> data/shortlist.json (rebuilt once per IST day).
    Each segment gets its own slots so Midcap 150 names are not crowded out by large caps."""
    import pandas as pd

    now = now_ist()
    uni = load_universe(refresh=True)
    fno_symbols(refresh=True)
    daily = _download([s + ".NS" for s in uni], "2mo", "1d")
    if len(daily) < 0.8 * len(uni):
        return {"error": f"Daily data incomplete ({len(daily)}/{len(uni)} symbols). Retry in a few minutes."}
    passed = {"LARGE": [], "MID": [], "SMALL": []}
    for y, d in daily.items():
        d = d[d.index.date < now.date()].tail(20)  # completed sessions only
        if len(d) < 15:
            continue
        prev = float(d["Close"].iloc[-1])
        turnover = float((d["Close"] * d["Volume"]).mean()) / 1e7
        tr = pd.concat([d["High"] - d["Low"], (d["High"] - d["Close"].shift()).abs(),
                        (d["Low"] - d["Close"].shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.tail(14).mean()) / prev * 100
        if prev >= MIN_PRICE and turnover >= MIN_TURNOVER_CR and atr >= MIN_ATR_PCT:
            sym = y[:-3]
            passed[uni[sym]["segment"]].append({"symbol": sym, "segment": uni[sym]["segment"],
                                                "industry": uni[sym]["industry"], "prev_close": _r(prev),
                                                "turnover_cr": _r(turnover, 1), "atr_pct": _r(atr)})
    rows = []
    for seg, items in passed.items():
        rows += sorted(items, key=lambda r: -r["turnover_cr"])[:SHORTLIST_CAPS[seg]]
    res = {"date": now.date().isoformat(), "universe": len(uni), "passed": {k: len(v) for k, v in passed.items()},
           "selected": {k: sum(r["segment"] == k for r in rows) for k in passed},
           "filters": {"min_price": MIN_PRICE, "min_turnover_cr": MIN_TURNOVER_CR, "min_atr_pct": MIN_ATR_PCT},
           "symbols": rows}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "shortlist.json"), "w") as f:
        json.dump(res, f)
    return res


def shortlist():
    cached = _load(os.path.join(DATA_DIR, "shortlist.json"))
    return cached if cached and cached.get("date") == now_ist().date().isoformat() else screen()


def regime():
    """Index backdrop: day change from the official index (the ETF is too thinly traded to trust), side of VWAP from
    the index's ETF because indices have no volume, plus India VIX."""
    etf = {x["symbol"]: x for x in get_quotes([NIFTY_ETF, MID_ETF])}
    nifty, mid, vix = nse_indices(["NIFTY 50", "NIFTY MIDCAP 150", "INDIA VIX"])
    today = now_ist().date()

    def side(x, idx):
        up = x.get("ltp") and x.get("vwap") and x["ltp"] > x["vwap"]
        official = idx and idx["stamp"].date() == today
        return {"chg_pct": idx["chg_pct"] if official else x.get("chg_pct"),
                "vs_vwap": None if not x.get("vwap") else "above" if up else "below",
                "src": "nse-live" if official else x.get("src")}

    if vix:
        vix = vix["last"]
    else:
        d = get_bars(["^INDIAVIX"], "5d", "15m").get("^INDIAVIX")
        vix = _r(d["Close"].iloc[-1]) if d is not None and not d.empty else None
    return {"nifty": side(etf[NIFTY_ETF], nifty), "midcap150": side(etf[MID_ETF], mid), "india_vix": vix}


def _candidates(frames, meta, now, p=None):
    """The day's ORB orders from 15-minute frames ({symbol: bars}) and the shortlist rows, ranked by opening-range
    relative volume. Pure, so the selftest and the backtester exercise the same rules as the live scan."""
    p = p or strategy.PARAMS
    end, out = strategy.or_end(p), []
    if now.time() < end:
        return out
    for sym, m in meta.items():
        d = frames.get(sym)
        if d is None or d.empty:
            continue
        days = [g for _, g in d.groupby(d.index.date)]
        cur = days[-1]
        if cur.index[-1].date() != now.date() or cur.index[0].time() != OPEN:
            continue
        orb, vols = cur[cur.index.time < end], []
        for g in days[:-1]:
            w = g[g.index.time < end]
            if not w.empty and w.index[0].time() == OPEN:
                vols.append(float(w["Volume"].sum()))
        plan, _ = strategy.opening_plan(float(orb["Open"].iloc[0]), float(orb["High"].max()), float(orb["Low"].min()),
                                        float(orb["Close"].iloc[-1]), float(orb["Volume"].sum()), vols,
                                        m["atr_pct"] / 100 * m["prev_close"], p)
        if not plan:
            continue
        after = cur[cur.index.time >= end]
        out.append({"symbol": sym, "segment": m["segment"], "industry": m["industry"], **plan,
                    "ltp": _r(cur["Close"].iloc[-1]), "atr_pct": m["atr_pct"], "turnover_cr": m["turnover_cr"],
                    "state": _state(plan, after["High"].max() if not after.empty else None,
                                    after["Low"].min() if not after.empty else None, float(cur["Close"].iloc[-1]), p)})
    return sorted(out, key=lambda c: -c["or_rvol"])


def _state(plan, hi, lo, ltp, p):
    """waiting: not triggered yet. triggered: traded through the trigger but still near it. extended: do not chase."""
    sgn = 1 if plan["side"] == "LONG" else -1
    if (ltp - plan["trigger"]) * sgn > p["extended_r"] * abs(plan["trigger"] - plan["stop"]):
        return "extended"
    ext = hi if sgn > 0 else lo
    return "triggered" if ext is not None and (ext - plan["trigger"]) * sgn >= 0 else "waiting"


def scan(top=None):
    """Today's opening-range orders on the shortlist: side, trigger and stop computed in strategy.py, the leaders
    re-priced from nse-live. Run it once the 09:15 candle is complete; the levels do not change after that."""
    p = strategy.PARAMS
    st = status()
    if st["state"] != "open":
        return {"error": st.get("error") or f"Market not open ({st['state']})", **st}
    now = now_ist()
    if now.time() < strategy.or_end(p):
        return {"error": f"The opening range completes at {strategy.or_end(p).strftime('%H:%M')} IST."}
    sl = shortlist()
    if "error" in sl:
        return sl
    meta = {r["symbol"]: r for r in sl["symbols"]}
    frames = {y[:-3]: d[d.index <= now] for y, d in _download([s + ".NS" for s in meta], "1mo", "15m").items()}
    last = max((d.index[-1] for d in frames.values() if not d.empty), default=None)
    if last is None or last.date() != now.date():
        return {"error": "No intraday bars for today yet; retry in two minutes. Do not trade without them."}
    age = (now - last).total_seconds() / 60
    if age > STALE_BAR_MIN + 15:
        return {"error": f"Stale bars: last bar {last.strftime('%H:%M')} IST, {age:.0f} min old."}

    reg = regime()
    cands = _candidates(frames, meta, now, p)[:top or p["top"]]
    for c, q in zip(cands, get_quotes([c["symbol"] for c in cands])):
        if q.get("ltp") and q.get("live"):
            c.update(ltp=q["ltp"], src=q["src"])
            # the trigger lies outside the opening range, so a day high or low beyond it means it has traded
            c["state"] = _state(c, q.get("high") or None, q.get("low") or None, q["ltp"], p)
        else:
            c["src"] = "yfinance"
        side = reg[REF[c["segment"]]]["vs_vwap"]
        c["with_index"] = None if side is None else (c["side"] == "LONG") == (side == "above")
        c["shortable"] = c["side"] == "LONG" or shortable(c["symbol"])
    return {"time_ist": now.strftime("%Y-%m-%d %H:%M"), "last_bar": last.strftime("%H:%M"), "regime": reg,
            "shortlist": len(meta), "entry_until": p["entry_until"], "candidates": cands}


def get_history(symbol, period="5d", interval="15m"):
    d = _download([_y(symbol)], period, interval).get(_y(symbol))
    if d is None:
        return {"error": f"No data for {symbol}"}
    fmt = "%Y-%m-%d %H:%M" if interval[-1] in "mh" else "%Y-%m-%d"
    data = [{"time": ts.strftime(fmt), "open": _r(b["Open"]), "high": _r(b["High"]), "low": _r(b["Low"]),
             "close": _r(b["Close"]), "volume": int(b["Volume"])} for ts, b in d.iterrows()]
    return {"symbol": symbol, "period": period, "interval": interval, "data": data}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="open / closed / pre_open / holiday")
    sub.add_parser("universe", help="Nifty 500 EQ constituents by segment and industry")
    sub.add_parser("screen", help="rebuild the liquid shortlist now")
    sub.add_parser("regime", help="Nifty 50 and Midcap 150 backdrop plus India VIX")
    s = sub.add_parser("scan", help="today's opening-range orders on the shortlist")
    s.add_argument("--top", type=int)
    sub.add_parser("price").add_argument("symbol")
    sub.add_parser("multi").add_argument("symbols", help="comma separated")
    h = sub.add_parser("history")
    h.add_argument("symbol")
    h.add_argument("--period", default="5d")
    h.add_argument("--interval", default="15m")
    a = p.parse_args()

    if a.cmd == "status":
        result = status()
    elif a.cmd == "universe":
        uni = load_universe(refresh=True)
        result = {"count": len(uni),
                  "segments": dict(Counter(v["segment"] for v in uni.values()).most_common()),
                  "industries": dict(Counter(v["industry"] for v in uni.values()).most_common())}
    elif a.cmd == "screen":
        result = screen()
    elif a.cmd == "regime":
        result = regime()
    elif a.cmd == "scan":
        result = scan(a.top)
    elif a.cmd == "price":
        result = get_quotes([a.symbol])[0]
    elif a.cmd == "multi":
        result = get_quotes(a.symbols.split(","))
    else:
        result = get_history(a.symbol, a.period, a.interval)
    # scan output is read every 15 minutes by the agent, so keep it compact
    print(json.dumps(result, separators=(",", ":")) if a.cmd == "scan" else json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
