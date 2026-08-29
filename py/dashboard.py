"""E2/E5 local dashboard (FastAPI) -- read-only views over data/paper.db and
data/market.db. Binds to 127.0.0.1 only (enforced by scripts/dashboard.bat's
uvicorn invocation, and defensively again in this file's __main__ block).
No writes anywhere in this module: every DB connection is opened in
SQLite's own read-only URI mode (`mode=ro`) so a coding mistake here cannot
mutate either database, not just "we only happen to write SELECT queries".
No Testnet, no keys, no parameter panel, no strategy switcher.

E5 adds: richer /api/summary (ret_pct/position/health per symbol),
reason_text on closed trades + live unrealized/stop-distance on the open
trade, /api/ohlc (binance spot 4h candles) and /api/events (a human-readable
entry/exit/reverse + aggregated-silence narrative over paper_signals +
paper_trades). All of it is derived read-only from the existing schema --
no new tables, no writes.

E15 adds three display blocks to /api/summary (2026-08-28): per-symbol
`trigger` (distance-to-entry: band distances + the squeeze gate's own
next-bar formula mirrored from strategy_squeeze), per-symbol `managed`
(exit-rule state for an open position: bars held, stall-exit countdown,
breakeven-locked -- all derived from paper_state + the deployed batch #8
constants, since r0 itself is never persisted), and top-level `exit_config`.
Same discipline: read-only derivations, no schema change, no engine change.
"""
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db as market_db
import paper_loop as pl
import paper_store as ps
import perf

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Paper Trading Dashboard (E2/E5, engineering validation only)")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PAPER_STATE_COLS = ["last_ts", "cash", "position_dir", "qty", "entry_px", "entry_ts",
                     "stop_disp", "equity", "updated_at"]
TRADE_COLS = ["entry_ts", "exit_ts", "direction", "qty", "entry_px", "exit_px", "fees", "funding",
              "pnl", "reason"]
SIGNAL_COLS = ["ts", "close", "upper", "lower", "width_rank", "sqz_ok", "long_sig", "short_sig",
               "position", "stop_disp", "action", "reason"]
EVENT_TRADE_COLS = ["entry_ts", "exit_ts", "direction", "entry_px", "exit_px", "pnl", "reason"]

CASH0 = pl.CASH0


def _gap_tolerance(symbol):
    # stop-exit fills already carry engine.py's own 2*tick slip (see
    # engine.py's sell_fill/buy_fill); this tolerance = that slip + a 2-tick
    # buffer -- per-symbol since E13's real tick sizes -- so only an actual
    # open-gap-through-the-stop (not the routine fill slip) trips the
    # "跳空開盤" annotation.
    return 4 * pl.tick_for(symbol)

# E9: a check is considered overdue if no symbol's paper_state has been
# touched in this many hours (ticks run every 4h -- 5h gives one missed
# tick's worth of slack before flagging).
LAST_CHECK_OVERDUE_HOURS = 5

# E10: /api/ohlc's live-quote proxy. Display-layer only -- this hits
# Binance's public GET /api/v3/klines (no key, no auth) purely to draw the
# price chart at finer resolutions than market.db stores; it never writes to
# market.db or any other database. On network failure, 4h/1d fall back to
# the existing market.db query (source="db"); 15m/1h have no db equivalent
# and report {"error": "live_unavailable"} instead of raising.
OHLC_TFS = ("15m", "1h", "4h", "1d")
OHLC_CACHE_TTL_SEC = 30.0
OHLC_LIVE_LIMIT = 220
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_OHLC_CACHE = {}  # (symbol, tf) -> (monotonic_fetch_time, payload_dict)


def _ro_connect(path):
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=503, detail=f"database not found yet: {p}")
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def paper_con():
    return _ro_connect(ps.DB_PATH)


def market_con():
    return _ro_connect(market_db.DB_PATH)


def _validate_symbol(symbol):
    if symbol not in pl.SYMBOLS:
        raise HTTPException(status_code=400, detail=f"unknown symbol: {symbol!r}, expected one of {pl.SYMBOLS}")


def _fmt(v, digits=2):
    if v is None:
        return "-"
    return f"{v:,.{digits}f}"


def _fmt_signed(v, digits=2):
    if v is None:
        return "-"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.{digits}f}"


def _dirword(direction):
    return "多" if direction == "long" else "空"


def _stop_ratchet(con, symbol, entry_ts, exit_ts):
    """Non-null stop_disp values from paper_signals over [entry_ts, exit_ts]
    (the exit bar itself is null on a pure stop-exit -- open_pos is cleared
    before stop_disp is computed for that bar -- so the last non-null value
    here is effectively the previous bar's stop, which is the level that
    actually got touched). Returns (first, last, ratchet_move_count)."""
    cur = con.execute(
        "SELECT stop_disp FROM paper_signals WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
        (symbol, entry_ts, exit_ts))
    vals = [r[0] for r in cur.fetchall() if r[0] is not None]
    if not vals:
        return None, None, 0
    moves = sum(1 for i in range(1, len(vals)) if vals[i] != vals[i - 1])
    return vals[0], vals[-1], moves


def _build_reason_text(con, symbol, trade):
    """trade: dict with entry_ts, exit_ts, direction, exit_px, reason (raw
    engine.py exit_reason: 'stop' / 'reversal' / 'window_end' / None).
    Display-layer sentence only -- the raw `reason` column is never
    rewritten in paper.db."""
    reason = trade.get("reason")
    direction = trade["direction"]
    dirw = _dirword(direction)
    if reason == "stop":
        first_stop, last_stop, _moves = _stop_ratchet(con, symbol, trade["entry_ts"], trade["exit_ts"])
        if first_stop is None:
            return reason or ""
        move_word = "上移" if direction == "long" else "下移"
        cross_word = "下" if direction == "long" else "上"
        text = (f"{dirw}單觸及追蹤停損：停損自進場後首值 {_fmt(first_stop)} "
                f"一路棘輪{move_word}至 {_fmt(last_stop)}，本棒{cross_word}穿觸發，出場 {_fmt(trade['exit_px'])}")
        exit_px = trade.get("exit_px")
        if exit_px is not None and abs(exit_px - last_stop) > _gap_tolerance(symbol):
            text += "（跳空開盤，以開盤價成交）"
        return text
    elif reason == "stall":
        bars = None
        if trade.get("entry_ts") is not None and trade.get("exit_ts") is not None:
            bars = int((trade["exit_ts"] - trade["entry_ts"]) // pl.STEP_MS)
        gain_word = "低" if direction == "long" else "高"
        return (f"{dirw}單停滯出場：進場後第 {pl.STALL_BARS} 棒起逐棒檢查，"
                f"第 {bars if bars is not None else '?'} 棒收盤仍{gain_word}於進場價，"
                f"以收盤 {_fmt(trade['exit_px'])} 平倉（批次 #8 規則）")
    elif reason == "reversal":
        new_dir_word = "空" if direction == "long" else "多"
        return f"出現反向訊號，平倉並同棒反手做{new_dir_word}"
    elif reason == "window_end":
        return "重放窗末強制平倉"
    else:
        return reason or ""


def _build_event_items(con, symbol, sig_rows, entry_map, exit_map):
    """sig_rows: ascending list of paper_signals dicts (SIGNAL_COLS keys).
    entry_map/exit_map: {ts: trade dict} from paper_trades, keyed by
    entry_ts / exit_ts respectively. Returns newest-to-oldest mixed list of
    {type:'event', ts, kind, text, sub} and
    {type:'silence', from_ts, to_ts, n, text}."""
    items = []
    hold_buffer = []

    def flush_silence():
        if not hold_buffer:
            return
        n = len(hold_buffer)
        from_ts = hold_buffer[0]["ts"]
        to_ts = hold_buffer[-1]["ts"]
        holding = hold_buffer[0]["position"] is not None
        counts = {}
        order = []
        for r in hold_buffer:
            rtext = r["reason"] or ""
            if rtext not in counts:
                order.append(rtext)
            counts[rtext] = counts.get(rtext, 0) + 1
        reason_text = "、".join(f"{k} ×{counts[k]}" for k in order)
        label = "持倉未動作" if holding else "未進場"
        text = f"其間 {n} 根{label}：{reason_text}"
        if holding:
            stops = [r["stop_disp"] for r in hold_buffer if r["stop_disp"] is not None]
            if stops:
                moves = sum(1 for i in range(1, len(stops)) if stops[i] != stops[i - 1])
                move_word = "上移" if hold_buffer[0]["position"] == "long" else "下移"
                text += f"；停損{move_word} {moves} 次（{_fmt(stops[0])} → {_fmt(stops[-1])}）"
        else:
            ranks = [r["width_rank"] for r in hold_buffer if r["width_rank"] is not None]
            if ranks:
                text += f"（最接近 {_fmt(min(ranks), 1)}）"
        items.append(dict(type="silence", from_ts=from_ts, to_ts=to_ts, n=n, text=text))
        hold_buffer.clear()

    for row in sig_rows:
        if row["action"] == "hold":
            hold_buffer.append(row)
            continue
        flush_silence()

        if row["action"] == "entry":
            trade = entry_map.get(row["ts"])
            direction = row["position"]
            dirw = _dirword(direction)
            price = trade["entry_px"] if trade else row["close"]
            side = "突破上軌" if direction == "long" else "跌破下軌"
            level = row["upper"] if direction == "long" else row["lower"]
            main = f"做{dirw} {_fmt(price)}"
            sub = f"擠壓成立（布林通道寬度排名 {_fmt(row['width_rank'], 1)}）且收盤{side} {_fmt(level)}"
            items.append(dict(type="event", ts=row["ts"], kind="entry", text=main, sub=sub))

        elif row["action"] == "exit":
            trade = exit_map.get(row["ts"])
            direction = trade["direction"] if trade else None
            dirw = _dirword(direction)
            exit_px = trade["exit_px"] if trade else None
            pnl = trade["pnl"] if trade else None
            raw_reason = trade.get("reason") if trade else None
            if raw_reason == "stall":
                # batch #8 stall exit: closed AT the bar's close by the
                # stall rule -- it never touched the trailing stop, so the
                # stop-touch narrative below would be factually wrong here.
                bars = None
                if trade.get("entry_ts") is not None:
                    bars = int((row["ts"] - trade["entry_ts"]) // pl.STEP_MS)
                gain_word = "低" if direction == "long" else "高"
                main = f"{dirw}單停滯出場 {_fmt(exit_px)}，損益 {_fmt_signed(pnl)}"
                sub = (f"進場後第 {pl.STALL_BARS} 棒起逐棒檢查：本棒（第 {bars if bars is not None else '?'} 棒）"
                       f"收盤仍{gain_word}於進場價，依批次 #8 規則以收盤平倉")
            else:
                first_stop, last_stop, moves = _stop_ratchet(
                    con, symbol, trade["entry_ts"] if trade else row["ts"], row["ts"])
                move_word = "上移" if direction == "long" else "下移"
                cross_word = "下" if direction == "long" else "上"
                extreme_word = "最低" if direction == "long" else "最高"
                main = f"{dirw}單觸及追蹤停損 {_fmt(last_stop)}，出場 {_fmt(exit_px)}，損益 {_fmt_signed(pnl)}"
                sub = (f"停損自 {_fmt(first_stop)} 隨行情棘輪{move_word} {moves} 次；"
                       f"本棒{extreme_word} {_fmt(exit_px)} {cross_word}穿")
            items.append(dict(type="event", ts=row["ts"], kind="exit", text=main, sub=sub))

        elif row["action"] == "reverse":
            new_trade = entry_map.get(row["ts"])
            old_trade = exit_map.get(row["ts"])
            new_dir = new_trade["direction"] if new_trade else row["position"]
            old_dir = old_trade["direction"] if old_trade else ("short" if new_dir == "long" else "long")
            old_w = _dirword(old_dir)
            new_w = _dirword(new_dir)
            price = new_trade["entry_px"] if new_trade else row["close"]
            side = "突破上軌" if new_dir == "long" else "跌破下軌"
            level = row["upper"] if new_dir == "long" else row["lower"]
            main = f"平{old_w}單並反手做{new_w} {_fmt(price)}"
            sub = f"擠壓成立（布林通道寬度排名 {_fmt(row['width_rank'], 1)}）且收盤{side} {_fmt(level)}"
            if old_trade and old_trade.get("reason") == "stop":
                sub += f"；原{old_w}單同棒觸停損 {_fmt(old_trade['exit_px'])} 先出"
            elif old_trade and old_trade.get("reason") == "stall":
                sub += f"；原{old_w}單同棒停滯出場 {_fmt(old_trade['exit_px'])} 先出"
            items.append(dict(type="event", ts=row["ts"], kind="reverse", text=main, sub=sub))

    flush_silence()
    return list(reversed(items))


def _latest_signal_verdict(con, symbol):
    """E9: the single most recent paper_signals row for symbol, as raw
    {ts, action, reason} -- the human-readable "人話" text is a display-layer
    concern left to the frontend (it already has `position` from api_summary
    and the full /api/events text for entry/exit/reverse rows)."""
    cur = con.execute(
        "SELECT ts, action, reason FROM paper_signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (symbol,))
    row = cur.fetchone()
    if not row:
        return None
    return dict(zip(["ts", "action", "reason"], row))


def _trigger_status(con, symbol):
    """E15: how far the latest closed bar sits from the two entry conditions.
    gate_next mirrors strategy_squeeze's own next-bar gate formula exactly:
    `rank.rolling(sqz_win).min().shift(1) < sqz_thresh` evaluated at the next
    bar equals min(width_rank of the latest sqz_win stored bars) < sqz_thresh,
    and pandas' rolling(min_periods=window) yields NaN (-> gate closed) when
    any of those ranks is missing -- mirrored here by requiring a full window
    of non-null ranks. Band distances are measured against the latest CLOSED
    bar's own bands; the next bar's bands will differ slightly, so this is a
    display-layer estimate, labeled as such in the frontend.

    Squeeze-specific by construction: it mirrors THAT gate's formula. Under
    another strategy plugin the params simply are not there and this returns
    None, so the panel shows nothing rather than a number belonging to a
    rule that is not running (the generic version is the next step)."""
    if "sqz_win" not in pl.P or "sqz_thresh" not in pl.P:
        return None
    win = int(pl.P["sqz_win"])
    thresh = float(pl.P["sqz_thresh"])
    cur = con.execute(
        "SELECT ts, close, upper, lower, width_rank, sqz_ok FROM paper_signals "
        "WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, win))
    rows = cur.fetchall()
    if not rows:
        return None
    ts, close, upper, lower, width_rank, sqz_ok = rows[0]
    recent_ranks = [r[4] for r in rows]  # newest first
    known = [r for r in recent_ranks if r is not None]
    min_rank = min(known) if known else None
    gate_next = bool(len(known) == win and min_rank is not None and min_rank < thresh)
    dist_up_pct = (upper - close) / close * 100.0 \
        if (upper is not None and close is not None and close != 0) else None
    dist_dn_pct = (close - lower) / close * 100.0 \
        if (lower is not None and close is not None and close != 0) else None
    return dict(
        ts=ts, close=close, upper=upper, lower=lower,
        width_rank=width_rank, sqz_ok=bool(sqz_ok),
        recent_ranks=recent_ranks, min_rank=min_rank, gate_next=gate_next,
        dist_up_pct=dist_up_pct, dist_dn_pct=dist_dn_pct,
        sqz_win=win, sqz_thresh=thresh,
    )


def _stats_block(closed, equity_rows, cash0):
    """Serializable 策略表現 block. Ratios that are undefined (no losing
    trade, or no trades at all) come back as None -- the frontend renders
    them as ∞/—, because printing a number there would be a claim the data
    does not support."""
    stats = perf.compute_stats(closed)
    return dict(
        trades=stats["n"], wins=stats["n_win"], losses=stats["n_loss"],
        win_rate=stats["win_rate"] if stats["n"] else None,
        avg_win=stats["avg_win"], avg_loss=stats["avg_loss"],
        payoff=perf.payoff_ratio(stats), pf=perf.profit_factor(stats),
        net=stats["net"], net_pct=(stats["net"] / cash0 * 100.0) if cash0 else None,
        fees_total=stats["fees_total"], funding_total=stats["funding_total"],
        max_dd_pct=perf.max_drawdown_pct(equity_rows, cash0),
        cash0=cash0,
    )


def _closed_trades(con, symbol):
    rows = con.execute(
        "SELECT pnl, fees, funding FROM paper_trades "
        "WHERE symbol=? AND exit_ts IS NOT NULL AND pnl IS NOT NULL ORDER BY entry_ts",
        (symbol,)).fetchall()
    return [dict(pnl=r[0], fees=r[1], funding=r[2]) for r in rows]


def _performance(con, symbol):
    """Win rate / payoff / profit factor / drawdown for one account, derived
    read-only from paper.db through perf.py -- the same module
    tools/export_trades.py uses, so the dashboard and the exported workbook
    can never quote different numbers for the same trades. Open positions are
    excluded throughout: an unrealized gain is not a result yet."""
    equity_rows = con.execute(
        "SELECT ts, equity FROM paper_equity WHERE symbol=? ORDER BY ts", (symbol,)).fetchall()
    return _stats_block(_closed_trades(con, symbol), equity_rows, CASH0)


def _performance_pooled(con, symbols):
    """All accounts together. Trades pool trivially (they are independent
    events); the equity curve is each account's own curve forward-filled onto
    the union of timestamps and summed, seeded at its starting capital, so a
    total drawdown can be measured even if one symbol started later."""
    closed = []
    for symbol in symbols:
        closed.extend(_closed_trades(con, symbol))
    per, all_ts = {}, set()
    for symbol in symbols:
        rows = con.execute(
            "SELECT ts, equity FROM paper_equity WHERE symbol=? ORDER BY ts", (symbol,)).fetchall()
        per[symbol] = rows
        all_ts.update(r[0] for r in rows)
    idx = {s: 0 for s in symbols}
    last = {s: CASH0 for s in symbols}
    pooled = []
    for ts in sorted(all_ts):
        total = 0.0
        for symbol in symbols:
            rows = per[symbol]
            while idx[symbol] < len(rows) and rows[idx[symbol]][0] <= ts:
                last[symbol] = rows[idx[symbol]][1]
                idx[symbol] += 1
            total += last[symbol]
        pooled.append((ts, total))
    return _stats_block(closed, pooled, CASH0 * len(symbols))


def _managed_status(state):
    """E15: exit-rule snapshot for an OPEN position, derived from paper_state
    plus the deployed batch #8 constants only. r0 (= stop_mult * ATR at
    entry) is never persisted, so this reports only the forms that don't
    need it: the stall threshold price is exact only when STALL_GAIN == 0
    (then it is entry_px itself, r0-independent), and breakeven is reported
    as the observable outcome "displayed stop already at/past entry" (the
    stored stop_disp includes build_signal_rows' BE-floor mirror), not as
    the engine's internal armed flag. Returns None when flat."""
    if not state or not state.get("position_dir"):
        return None
    entry_ts, last_ts = state.get("entry_ts"), state.get("last_ts")
    bars_held = None
    if entry_ts is not None and last_ts is not None:
        bars_held = max(0, int((last_ts - entry_ts) // pl.STEP_MS))
    d = 1.0 if state["position_dir"] == "long" else -1.0
    stop, entry_px = state.get("stop_disp"), state.get("entry_px")
    breakeven_locked = None
    if stop is not None and entry_px is not None:
        breakeven_locked = bool(d * (stop - entry_px) >= 0.0)
    stall_threshold_px = entry_px if (pl.STALL_GAIN == 0.0 and entry_px is not None) else None
    stall_active = bool(bars_held is not None and bars_held >= pl.STALL_BARS)
    return dict(
        bars_held=bars_held, stall_bars=pl.STALL_BARS, stall_gain=pl.STALL_GAIN,
        stall_active=stall_active, stall_threshold_px=stall_threshold_px,
        be_trigger=pl.BE_TRIGGER, breakeven_locked=breakeven_locked,
    )


def _binance_symbol(symbol):
    return symbol.replace("/", "")


def _fetch_live_ohlc(symbol, tf):
    resp = httpx.get(
        BINANCE_KLINES_URL,
        params={"symbol": _binance_symbol(symbol), "interval": tf, "limit": OHLC_LIVE_LIMIT},
        timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()


def _live_bars(raw, now_ms):
    """raw: Binance's own kline row shape (open_ts, o, h, l, c, v, close_ts,
    ...). closed=True iff the bar's closeTime has already passed -- the last
    row returned by Binance is almost always still forming."""
    bars = []
    for row in raw:
        open_ts, o, h, l, c, v, close_ts = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        bars.append(dict(
            ts=int(open_ts), open=float(o), high=float(h), low=float(l),
            close=float(c), volume=float(v), closed=bool(int(close_ts) < now_ms),
        ))
    return bars


def _db_ohlc_bars(symbol, tf):
    """Network-failure fallback for tf in (4h, 1d): the pre-existing
    market.db query, unchanged in shape, just parameterized by tf instead of
    being hardcoded to pl.TIMEFRAME. All bars here are historical closed
    candles by construction."""
    con = market_con()
    try:
        epoch = pl.epoch_ms()
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        start_ms = max(epoch, now_ms - 30 * 24 * 3600 * 1000)
        cur = con.execute(
            "SELECT ts, open, high, low, close, volume FROM klines "
            "WHERE exchange=? AND symbol=? AND timeframe=? AND ts>=? ORDER BY ts ASC",
            (pl.EXCHANGE, symbol, tf, start_ms))
        rows = cur.fetchall()
    finally:
        con.close()
    cols = ["ts", "open", "high", "low", "close", "volume"]
    bars = [dict(zip(cols, r)) for r in rows]
    for b in bars:
        b["closed"] = True
    return bars


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/summary")
def api_summary():
    con_paper = paper_con()
    try:
        con_market = market_con()
    except HTTPException:
        con_paper.close()
        raise
    try:
        states = {}
        latest_verdicts = {}
        triggers = {}
        performances = {}
        for symbol in pl.SYMBOLS:
            cur = con_paper.execute(
                "SELECT last_ts, cash, position_dir, qty, entry_px, entry_ts, stop_disp, equity, updated_at "
                "FROM paper_state WHERE symbol=?", (symbol,))
            row = cur.fetchone()
            states[symbol] = dict(zip(PAPER_STATE_COLS, row)) if row else None
            latest_verdicts[symbol] = _latest_signal_verdict(con_paper, symbol)
            triggers[symbol] = _trigger_status(con_paper, symbol)
            performances[symbol] = _performance(con_paper, symbol)
        performance_all = _performance_pooled(con_paper, pl.SYMBOLS)
        health = pl.health_check(con_market, con_paper, pl.epoch_ms())
    finally:
        con_paper.close()
        con_market.close()

    # E9: heartbeat -- last_check_ts is the max updated_at across all three
    # symbols' paper_state rows (whichever symbol the tick loop touched
    # most recently); last_check_overdue flags when that's more than
    # LAST_CHECK_OVERDUE_HOURS old, so the frontend doesn't have to reason
    # about clock skew itself.
    last_check_dt = None
    for st in states.values():
        if st and st.get("updated_at"):
            try:
                dt = datetime.fromisoformat(st["updated_at"])
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if last_check_dt is None or dt > last_check_dt:
                last_check_dt = dt
    last_check_ts = last_check_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") \
        if last_check_dt is not None else None
    last_check_overdue = (
        last_check_dt is not None
        and (datetime.now(tz=timezone.utc) - last_check_dt) > timedelta(hours=LAST_CHECK_OVERDUE_HOURS)
    )

    latest_bar_ts = None
    for symbol, st in states.items():
        if st and st["last_ts"] is not None:
            if latest_bar_ts is None or st["last_ts"] > latest_bar_ts:
                latest_bar_ts = st["last_ts"]

    for symbol in pl.SYMBOLS:
        st = states[symbol]
        h = health["symbols"].get(symbol, dict(ok=False, reasons=["n/a"]))
        if st is None:
            states[symbol] = dict(
                equity=None, ret_pct=None, position=None,
                health_ok=h["ok"], health_reasons=h["reasons"],
                latest_verdict=latest_verdicts.get(symbol),
                trigger=triggers.get(symbol), managed=None,
            )
            continue
        st["ret_pct"] = (st["equity"] / CASH0 - 1.0) if st["equity"] is not None else None
        if st["position_dir"]:
            st["position"] = dict(
                dir=st["position_dir"], entry_px=st["entry_px"], entry_ts=st["entry_ts"],
                stop=st["stop_disp"], unrealized=st["equity"] - st["cash"],
            )
        else:
            st["position"] = None
        st["health_ok"] = h["ok"]
        st["health_reasons"] = h["reasons"]
        st["latest_verdict"] = latest_verdicts.get(symbol)
        st["trigger"] = triggers.get(symbol)
        st["managed"] = _managed_status(st)
        st["performance"] = performances.get(symbol)

    return dict(
        announcement=pl.ANNOUNCEMENT,
        strategy=pl.strategies.describe(pl.STRATEGY, pl.P),
        performance_all=performance_all,
        cost_assumption=dict(
            fee_per_side=pl.FEE, slippage_ticks=2, tick_sizes=pl.TICKS,
            funding="binanceusdm 實際資金費率逐期結算（多方付正費率、空方收）",
            note="E13 成本真實化：幣安 USDT-M VIP0 taker；tick 依商品別；資金費率取 market.db 實際歷史值",
        ),
        cost_note=(f"成本假設：手續費 {pl.FEE * 100:.3f}%/邊（幣安 VIP0 taker）、滑價 2 跳（"
                    + "、".join(f"{s.split('/')[0]} {t}" for s, t in pl.TICKS.items())
                    + "）、資金費率逐期實際結算"),
        paper_epoch=pl.PAPER_EPOCH,
        cash0=CASH0,
        exit_config=dict(stall_bars=pl.STALL_BARS, stall_gain=pl.STALL_GAIN,
                          be_trigger=pl.BE_TRIGGER),
        server_time_utc=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        latest_bar_ts=latest_bar_ts,
        last_check_ts=last_check_ts,
        last_check_overdue=last_check_overdue,
        symbols=states,
        health=health,
    )


@app.get("/api/equity")
def api_equity(symbol: str = Query(...)):
    _validate_symbol(symbol)
    con = paper_con()
    try:
        cur = con.execute("SELECT ts, equity FROM paper_equity WHERE symbol=? ORDER BY ts ASC", (symbol,))
        rows = cur.fetchall()
    finally:
        con.close()
    return [dict(ts=r[0], equity=r[1]) for r in rows]


@app.get("/api/trades")
def api_trades(symbol: str = Query(...)):
    _validate_symbol(symbol)
    con = paper_con()
    try:
        cur = con.execute(
            "SELECT entry_ts, exit_ts, direction, qty, entry_px, exit_px, fees, funding, pnl, reason "
            "FROM paper_trades WHERE symbol=? ORDER BY entry_ts DESC", (symbol,))
        rows = cur.fetchall()

        last_close = current_stop = None
        cur2 = con.execute(
            "SELECT close, stop_disp FROM paper_signals WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,))
        r2 = cur2.fetchone()
        if r2:
            last_close, current_stop = r2

        out = []
        for r in rows:
            d = dict(zip(TRADE_COLS, r))
            if d["exit_ts"] is None:
                sign = 1.0 if d["direction"] == "long" else -1.0
                d["last_close"] = last_close
                d["current_stop"] = current_stop
                if last_close is not None:
                    d["unrealized"] = sign * d["qty"] * (last_close - d["entry_px"])
                else:
                    d["unrealized"] = None
                if last_close is not None and current_stop is not None:
                    diff = (last_close - current_stop) if d["direction"] == "long" else (current_stop - last_close)
                    d["dist_to_stop_pct"] = diff / last_close * 100.0 if last_close else None
                else:
                    d["dist_to_stop_pct"] = None
            else:
                d["reason_text"] = _build_reason_text(con, symbol, d)
            out.append(d)
    finally:
        con.close()
    return out


@app.get("/api/signals")
def api_signals(symbol: str = Query(...), limit: int = Query(200, ge=1, le=5000),
                 only_action: int = Query(0, ge=0, le=1)):
    _validate_symbol(symbol)
    con = paper_con()
    try:
        if only_action:
            cur = con.execute(
                "SELECT ts, close, upper, lower, width_rank, sqz_ok, long_sig, short_sig, "
                "position, stop_disp, action, reason FROM paper_signals "
                "WHERE symbol=? AND action != 'hold' ORDER BY ts DESC LIMIT ?", (symbol, limit))
        else:
            cur = con.execute(
                "SELECT ts, close, upper, lower, width_rank, sqz_ok, long_sig, short_sig, "
                "position, stop_disp, action, reason FROM paper_signals "
                "WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit))
        rows = cur.fetchall()
    finally:
        con.close()
    return [dict(zip(SIGNAL_COLS, r)) for r in rows]


@app.get("/api/ohlc")
def api_ohlc(symbol: str = Query(...), tf: str = Query("4h")):
    """E10: display-layer price chart, now with a tf switch (15m/1h/4h/1d).
    Source is Binance's public live klines proxy (see _fetch_live_ohlc);
    market.db is only ever a network-failure fallback for 4h/1d (source="db"
    in the response), and only 15m/1h have no such fallback since market.db
    never stores those resolutions -- those report {"error":"live_unavailable"}
    with a 200 rather than raising. Never writes to any database."""
    _validate_symbol(symbol)
    if tf not in OHLC_TFS:
        raise HTTPException(status_code=400, detail=f"unknown tf: {tf!r}, expected one of {OHLC_TFS}")

    cache_key = (symbol, tf)
    now_mono = time.monotonic()
    cached = _OHLC_CACHE.get(cache_key)
    if cached is not None and (now_mono - cached[0]) < OHLC_CACHE_TTL_SEC:
        return cached[1]

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    try:
        raw = _fetch_live_ohlc(symbol, tf)
        payload = dict(symbol=symbol, tf=tf, source="live", bars=_live_bars(raw, now_ms))
    except Exception:
        if tf in ("4h", "1d"):
            payload = dict(symbol=symbol, tf=tf, source="db", bars=_db_ohlc_bars(symbol, tf))
        else:
            return dict(error="live_unavailable")

    _OHLC_CACHE[cache_key] = (now_mono, payload)
    return payload


@app.get("/api/events")
def api_events(symbol: str = Query(...)):
    _validate_symbol(symbol)
    con = paper_con()
    try:
        cur = con.execute(
            "SELECT ts, close, upper, lower, width_rank, sqz_ok, long_sig, short_sig, "
            "position, stop_disp, action, reason FROM paper_signals WHERE symbol=? ORDER BY ts ASC",
            (symbol,))
        sig_rows = [dict(zip(SIGNAL_COLS, r)) for r in cur.fetchall()]

        cur2 = con.execute(
            "SELECT entry_ts, exit_ts, direction, entry_px, exit_px, pnl, reason "
            "FROM paper_trades WHERE symbol=? ORDER BY entry_ts ASC", (symbol,))
        trades = [dict(zip(EVENT_TRADE_COLS, r)) for r in cur2.fetchall()]
        entry_map = {t["entry_ts"]: t for t in trades}
        exit_map = {t["exit_ts"]: t for t in trades if t["exit_ts"] is not None}

        items = _build_event_items(con, symbol, sig_rows, entry_map, exit_map)
    finally:
        con.close()
    return items


@app.get("/api/health")
def api_health():
    con_paper = paper_con()
    try:
        con_market = market_con()
    except HTTPException:
        con_paper.close()
        raise
    try:
        counts = ps.table_counts(con_paper)
        health = pl.health_check(con_market, con_paper, pl.epoch_ms())
    finally:
        con_paper.close()
        con_market.close()
    return dict(table_counts=counts, health=health)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
