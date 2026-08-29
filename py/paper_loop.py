"""Paper-trading tick loop -- engineering validation only, NOT strategy
proof. The built-in squeeze is a known-no-edge placeholder rule (D-008);
simulated P&L here does not argue for deployment (D-013), and neither does
anyone else's plugin. The entry rule comes from strategies/ (see the
"strategy plugin" block below and strategies/__init__.py for the contract);
engine.run(), fetch_klines.fetch_series(), db.py and indicators.py are
reused verbatim. market.db is only ever written to via fetch_klines'
fetch_series() and fetch_futures' fetch_funding_series() resumable paths;
all paper state goes to the separate data/paper.db via paper_store.py.

Cost model (E13 成本真實化): fee 0.0005/side == Binance USDT-M VIP0 taker
0.05% -- CAL-002's placeholder happened to equal the real standard rate, so
the value stays but is now the documented actual schedule (edit FEE for a
VIP tier or BNB fee discount, e.g. 0.00045). Slippage keeps CAL-002's
2-tick convention but with per-symbol REAL Binance USDS-M futures tick
sizes (TICKS below) instead of a uniform 0.1, which overstated ETH/SOL
slippage 10x. Perpetual funding settles from market.db's actual binanceusdm
rate history: refresh_funding() extends it each tick (resumable), and
load_funding() normalizes Binance's millisecond-jittered settlement
timestamps onto the 4h grid for engine.run(funding=...). Documented proxy:
prices are binance SPOT 4h klines while funding is the USDS-M perpetual's
-- same three underlyings, engineering-validation frame (D-013) unchanged.

Exit config (batch #8, deployed 2026-08-26): run_tick() passes the
STALL_BARS/STALL_GAIN/BE_TRIGGER constants below into engine.run() -- V1
stall exit (close below entry from 6 bars after entry -> exit at close,
reason "stall") plus V3 breakeven stop floor (armed at +1R0 close-gain).
Judged per docs/BATCH_PLAN's frozen batch #8 rules; the squeeze signal rule
itself is unchanged and remains the known-no-edge placeholder. E14: each
tick also rolls the ~30-day-retention OI / taker-ratio collectors forward
(refresh_oi) -- pure future-research data, nothing in the replay reads it.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import db
import engine
import indicators as ind
import paper_store as ps
import strategies
from strategy_squeeze import build_signals

# ---- user config (repo-root config.json) -----------------------------------
# Symbols / ticks / epoch / cash / fee are read from config.json when it
# exists, so users pick their own watchlist without editing code. Timeframe
# stays frozen at 4h: the funding-settlement grid (grid_ts / FUNDING_MS) and
# the rule's own semantics are built around 4h closes. Missing file or keys
# fall back to the original three-symbol deployment defaults, keeping every
# existing test expectation valid verbatim.
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


def _load_config():
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError) as e:
        print(f"[config] WARNING: {CONFIG_PATH.name} unreadable ({e}); using built-in defaults")
    return {}


_CFG = _load_config()
_CFG_SYMBOLS = [s for s in _CFG.get("symbols", []) if isinstance(s, dict) and s.get("symbol")]

SYMBOLS = [s["symbol"] for s in _CFG_SYMBOLS] or ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
EXCHANGE = "binance"
TIMEFRAME = "4h"
STEP_MS = 4 * 3600 * 1000
WARMUP_BARS = 200
PAPER_EPOCH = str(_CFG.get("paper_epoch", "2026-08-01 00:00"))  # deploy epoch; warmup gives a non-blank dashboard
CASH0 = float(_CFG.get("cash0", 10_000.0))
FEE = float(_CFG.get("fee", 0.0005))  # Binance USDT-M VIP0 taker 0.05%/side (0.00045 with BNB discount)
TICK = 0.1      # generic fallback tick for symbols not in TICKS (synthetic test series) -- CAL-002 convention
TICKS = {s["symbol"]: float(s["tick"]) for s in _CFG_SYMBOLS if "tick" in s} or {
    # real Binance USDS-M futures tick sizes (E13)
    "BTC/USDT": 0.1,
    "ETH/USDT": 0.01,
    "SOL/USDT": 0.01,
}
FUNDING_MS = 8 * 3600 * 1000          # standard Binance funding interval
FUNDING_EXCHANGE = "binanceusdm"      # funding rows' exchange key in market.db
FUNDING_START = "2020-10-01"          # matches fetch_futures.FUNDING_START

# ---- strategy plugin -------------------------------------------------------
# Which rule decides the entries, and with what parameters. config.json's
# "strategy": {"module": ..., "params": {...}} picks any file in strategies/;
# the default is the built-in squeeze breakout, so an untouched config
# reproduces the original deployment exactly. An unknown module name or an
# unknown parameter key raises here rather than silently doing nothing --
# see strategies/__init__.py for the whole contract.
BUILTIN_STRATEGY = "squeeze_breakout"
_CFG_STRATEGY = _CFG.get("strategy") or {}
STRATEGY_NAME = str(_CFG_STRATEGY.get("module", BUILTIN_STRATEGY))
STRATEGY = strategies.load(STRATEGY_NAME)
P = strategies.resolve_params(STRATEGY, _CFG_STRATEGY.get("params"))

# Exit config: batch #8's deployed values are the defaults (V1 stall exit +
# V3 breakeven floor, judged 2026-08-26 on the built-in rule), overridable
# per-deployment via config.json's "exits" block -- null disables one.
# process_symbol() itself still defaults to the legacy exit config (all None)
# so the synthetic-series tests' direct engine.run comparisons stay valid
# verbatim; only run_tick() passes these.
_CFG_EXITS = _CFG.get("exits") or {}


def _cfg_num(block, key, default, cast):
    val = block.get(key, default)
    return None if val is None else cast(val)


STALL_BARS = _cfg_num(_CFG_EXITS, "stall_bars", 6, int)      # eligible from N bars after entry ...
STALL_GAIN = _cfg_num(_CFG_EXITS, "stall_gain", 0.0, float)  # ... exit if close-gain < N*R0
BE_TRIGGER = _cfg_num(_CFG_EXITS, "be_trigger", 1.0, float)  # floor the stop at entry at +N*R0

def _announcement():
    """Names the rule that is actually running. The built-in one keeps its
    original disclosure -- it is a known-no-edge placeholder (D-008) and its
    simulated P&L argues nothing (D-013); a user's own plugin gets the
    general form of the same warning."""
    label = f"{getattr(STRATEGY, 'NAME', STRATEGY_NAME)}（{STRATEGY_NAME}）"
    if STRATEGY_NAME == BUILTIN_STRATEGY:
        return f"[模擬帳戶] 規則：{label}－內建示範規則，已知無邊際（D-008），模擬損益不作策略論證（D-013）"
    return f"[模擬帳戶] 規則：{label}－模擬損益只反映這條規則本身，不構成任何策略論證"


ANNOUNCEMENT = _announcement()


def epoch_ms():
    return int(datetime.strptime(PAPER_EPOCH, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def tick_for(symbol):
    return TICKS.get(symbol, TICK)


def funding_symbol(symbol):
    """spot 'BTC/USDT' -> ccxt USDS-M perpetual notation 'BTC/USDT:USDT'."""
    return f"{symbol}:{symbol.split('/')[1]}"


def grid_ts(ts):
    """Nearest 4h-grid instant for a raw funding timestamp. Binance's stored
    history carries 1..47ms of forward jitter (measured); SOL's 2022-11
    sub-8h settlement stretch rounds half-up onto the grid."""
    return int(((int(ts) + STEP_MS // 2) // STEP_MS) * STEP_MS)


def refresh_klines(con):
    """Step 2: pull the latest closed bars via fetch_klines.py's own
    resumable fetch_series() -- unmodified reuse. Network failure does not
    abort the tick; falls back to whatever is already in market.db and
    reports the failure per symbol."""
    import fetch_klines as fk
    results = {}
    try:
        import ccxt
        ex = ccxt.binance({"enableRateLimit": True})
    except Exception as e:
        for symbol in SYMBOLS:
            results[symbol] = f"SKIPPED (ccxt unavailable: {e})"
        return results
    for symbol in SYMBOLS:
        try:
            added = fk.fetch_series(con, ex, EXCHANGE, symbol, TIMEFRAME, fk.date_ms("2019-01-01"))
            results[symbol] = f"OK (+{added} bars)"
        except Exception as e:
            results[symbol] = f"FAILED ({e}) -- using existing market.db data"
    return results


def refresh_funding(con):
    """Step 2b: extend market.db's binanceusdm funding history via
    fetch_futures.py's own resumable fetch_funding_series() -- unmodified
    reuse, perpetual symbols mapped from the spot ones. Network failure does
    not abort the tick; the replay then just has no rates beyond what is
    stored, which health_check reports as funding-data lag."""
    import fetch_futures as ff
    from fetch_klines import date_ms
    results = {}
    try:
        import ccxt
        ex = ccxt.binanceusdm({"enableRateLimit": True})
    except Exception as e:
        for symbol in SYMBOLS:
            results[symbol] = f"SKIPPED (ccxt unavailable: {e})"
        return results
    for symbol in SYMBOLS:
        try:
            added = ff.fetch_funding_series(con, ex, FUNDING_EXCHANGE, funding_symbol(symbol),
                                            date_ms(FUNDING_START))
            results[symbol] = f"OK (+{added} rows)"
        except Exception as e:
            results[symbol] = f"FAILED ({e}) -- using existing funding data"
    return results


def refresh_oi(con):
    """Step 2c (E14): roll the ~30-day-retention open-interest / taker-ratio
    collectors forward via fetch_oi.py's resumable paths. Pure data
    collection for future research (BATCH_PLAN E14 note) -- nothing in the
    replay reads these tables. Network failure never aborts the tick."""
    import fetch_oi as fo
    results = {}
    try:
        import ccxt
        ex = ccxt.binanceusdm({"enableRateLimit": True})
    except Exception as e:
        for symbol in SYMBOLS:
            results[symbol] = f"SKIPPED (ccxt unavailable: {e})"
        return results
    for symbol in SYMBOLS:
        perp = funding_symbol(symbol)
        try:
            a = fo.fetch_oi_series(con, ex, fo.EXCHANGE_ID, perp)
            b = fo.fetch_taker_series(con, ex, fo.EXCHANGE_ID, perp)
            results[symbol] = f"OK (oi +{a}, taker +{b})"
        except Exception as e:
            results[symbol] = f"FAILED ({e}) -- collection resumes next tick"
    return results


def load_df(con, symbol, start_ms):
    cur = con.execute(
        "SELECT ts, open, high, low, close, volume FROM klines "
        "WHERE exchange=? AND symbol=? AND timeframe=? AND ts>=? ORDER BY ts",
        (EXCHANGE, symbol, TIMEFRAME, start_ms))
    return pd.DataFrame(cur.fetchall(), columns=["ts", "Open", "High", "Low", "Close", "Volume"])


def load_funding(con, symbol, start_ms):
    """Engine-ready funding dict {4h bar-open ts: rate} from market.db's
    binanceusdm history (perpetual symbol mapped via funding_symbol()). Raw
    timestamps are normalized onto the 4h grid via grid_ts(); collisions
    after normalization (historically only SOL's 2022-11 sub-8h settlement
    stretch) sum their rates, preserving total cashflow."""
    out = {}
    cur = con.execute(
        "SELECT ts, rate FROM funding WHERE exchange=? AND symbol=? AND ts>=? ORDER BY ts",
        (FUNDING_EXCHANGE, funding_symbol(symbol), start_ms))
    for ts, rate in cur.fetchall():
        g = grid_ts(ts)
        out[g] = out.get(g, 0.0) + float(rate)
    return out


def compute_signal_detail(df, start_ms, end_ms, params):
    """The built-in squeeze rule written a SECOND time, independently, from
    indicators.py's exposed primitives only. Its whole purpose is to
    disagree: process_symbol() cross-checks it bar-by-bar against what
    strategies/squeeze_breakout.py produced and refuses to write anything to
    paper.db on the first divergence. Only meaningful for the built-in rule
    -- a user's own plugin cannot be asked to implement itself twice, and
    gets strategies.check_replay_stability()'s generic proof instead."""
    close = df["Close"]
    basis, upper, lower = ind.bollinger(close, params["bb_len"], params["bb_mult"])
    width = ind.bb_width_pct(basis, upper, lower)
    rank = ind.percentrank_prev(width, params["rank_len"])
    sqz_ok = rank.rolling(params["sqz_win"]).min().shift(1) < params["sqz_thresh"]
    in_win = (df["ts"] >= start_ms) & (df["ts"] <= end_ms)
    cross_up = ind.crossover(close, upper)
    cross_dn = ind.crossunder(close, lower)
    out = df.copy()
    out["upper"] = upper
    out["lower"] = lower
    out["width_rank"] = rank
    out["sqz_ok"] = sqz_ok
    out["cross_up"] = cross_up
    out["cross_dn"] = cross_dn
    out["long_sig"] = cross_up & sqz_ok & in_win
    out["short_sig"] = cross_dn & sqz_ok & in_win
    out["atr"] = ind.atr_rma(df["High"], df["Low"], close, params["atr_len"])
    out["in_win"] = in_win
    return out


def _isnan(x):
    return x is None or (isinstance(x, float) and x != x)


def _num(arr, i):
    """Storable float from an optional column (None array -> NULL)."""
    if arr is None:
        return None
    return None if _isnan(float(arr[i])) else float(arr[i])


def build_signal_rows(symbol, detail_df, trades_df, cash0, fee, epoch, funding_rates=None,
                      be_trigger=None):
    """Single pass over detail_df (ts-ordered), cross-referencing
    engine.run()'s already-computed trades_df to derive action/reason/
    position/stop_disp/equity per bar. Pure bookkeeping over engine.py's own
    decisions (fee/pnl values it already returned) -- no new trading logic;
    entry_fee is recomputed as qty*entry_px*fee only because engine.run()
    folds entry+exit fees into a single `fees` total on closed trades and
    doesn't expose them separately. funding_rates (same dict handed to
    engine.run(funding=...)) mirrors engine.py's step-0 settlements onto the
    cash path here -- same eligibility (entered at bar <= i-2, rate keyed
    exactly at the bar's open ts) -- so per-bar equity between settlements
    tracks the engine's own cash trajectory; the exit-bar update then adds
    back entry_fee AND the already-deducted funding, because trades_df's pnl
    is net of both. Returns (signal_rows, equity_rows, state).
    Only bars with ts >= epoch are written (warmup bars are indicator
    scaffolding only, per PAPER_EPOCH design -- no trade can occur there
    since build_signals()'s in_win gate already excludes them)."""
    def _opt(col, dtype=float):
        """Columns only the built-in squeeze rule produces. A different
        plugin simply leaves them out and they are stored as NULL -- the
        dashboard then has nothing to draw for them, which is the honest
        outcome, rather than showing another rule's numbers."""
        return detail_df[col].to_numpy(dtype=dtype) if col in detail_df.columns else None

    ts = detail_df["ts"].to_numpy()
    open_ = detail_df["Open"].to_numpy(dtype=float)
    close = detail_df["Close"].to_numpy(dtype=float)
    upper = _opt("upper")
    lower = _opt("lower")
    width_rank = _opt("width_rank")
    sqz_ok = _opt("sqz_ok", bool)
    long_sig = detail_df["long_sig"].to_numpy(dtype=bool)
    short_sig = detail_df["short_sig"].to_numpy(dtype=bool)
    in_win = detail_df["in_win"].to_numpy(dtype=bool)
    stop_dist = detail_df["stop_dist"].to_numpy(dtype=float) if "stop_dist" in detail_df.columns \
        else P.get("stop_mult", 2.0) * detail_df["atr"].to_numpy(dtype=float)
    rule_reason = _opt("reason", object)
    n = len(ts)

    entries_by_ts = {}
    exits_by_ts = {}
    for r in trades_df.itertuples():
        entries_by_ts[int(r.entry_ts)] = r
        if not _isnan(r.exit_ts):
            exits_by_ts[int(r.exit_ts)] = r

    signal_rows = []
    equity_rows = []
    cash = cash0
    open_pos = None  # dict(direction, qty, entry_px, entry_fee, stop_disp)

    for i in range(n):
        t = int(ts[i])
        position_before = open_pos["direction"] if open_pos else None
        action = "hold"
        reason = None

        # funding settlement mirror of engine.py's step 0 (same eligibility:
        # entered at bar <= i-2; rate keyed exactly at this bar's open ts),
        # BEFORE exit handling, so a stop-out bar still pays this instant's
        # funding -- keeps the per-bar cash path identical to the engine's.
        if funding_rates and open_pos is not None and (i - open_pos["entry_i"]) >= 2:
            rate = funding_rates.get(t)
            if rate is not None:
                fdir = 1.0 if open_pos["direction"] == "long" else -1.0
                amt = fdir * open_pos["qty"] * open_[i] * rate
                cash -= amt
                open_pos["funding_paid"] += amt

        # exit first, so a same-bar reversal (engine closes then opens at
        # the same ts) is attributed correctly
        if open_pos is not None and t in exits_by_ts:
            r = exits_by_ts[t]
            # r.pnl is net of both fees AND funding; entry_fee and the
            # periodic funding already left `cash` while the trade was open,
            # so both are added back to avoid double-counting them.
            cash += open_pos["entry_fee"] + open_pos["funding_paid"] + float(r.pnl)
            action = "exit"
            reason = f"exit {open_pos['direction']} @ {float(r.exit_px):.6f} ({r.exit_reason})"
            open_pos = None

        if t in entries_by_ts:
            r = entries_by_ts[t]
            entry_fee = float(r.qty) * float(r.entry_px) * fee
            cash -= entry_fee
            if action == "exit":
                action = "reverse"
                reason = f"reverse {position_before}->{r.direction} @ {float(r.entry_px):.6f}"
            else:
                action = "entry"
                reason = f"entry {r.direction} @ {float(r.entry_px):.6f}"
            open_pos = dict(direction=r.direction, qty=float(r.qty), entry_px=float(r.entry_px),
                              entry_ts=t, entry_i=i, entry_fee=entry_fee, funding_paid=0.0,
                              stop_disp=None, be_armed=False,
                              r0=None if _isnan(stop_dist[i]) else float(stop_dist[i]))

        if action == "hold":
            # 窗前暖機 and 已持倉同向 are framework facts (the epoch window and
            # the open position), so they stay here; everything else is the
            # rule explaining itself through the plugin's `reason` column.
            if not in_win[i]:
                reason = "窗前暖機"
            elif (long_sig[i] and position_before == "long") or \
                    (short_sig[i] and position_before == "short"):
                reason = "已持倉同向"
            elif rule_reason is not None and rule_reason[i] is not None \
                    and rule_reason[i] == rule_reason[i]:
                reason = str(rule_reason[i])
            else:
                reason = "無訊號"

        stop_disp = None
        if open_pos is not None and not _isnan(stop_dist[i]):
            if open_pos["direction"] == "long":
                candidate = close[i] - stop_dist[i]
                open_pos["stop_disp"] = candidate if open_pos["stop_disp"] is None \
                    else max(open_pos["stop_disp"], candidate)
            else:
                candidate = close[i] + stop_dist[i]
                open_pos["stop_disp"] = candidate if open_pos["stop_disp"] is None \
                    else min(open_pos["stop_disp"], candidate)
            # breakeven floor mirror of engine.py's 2b (batch #8 V3): arm on
            # a post-entry bar whose close-gain reaches be_trigger*R0, then
            # never display a stop on the losing side of the entry price.
            if be_trigger is not None and open_pos["r0"] is not None:
                d_ = 1.0 if open_pos["direction"] == "long" else -1.0
                if not open_pos["be_armed"] and i > open_pos["entry_i"] and \
                        d_ * (close[i] - open_pos["entry_px"]) >= be_trigger * open_pos["r0"]:
                    open_pos["be_armed"] = True
                if open_pos["be_armed"]:
                    if open_pos["direction"] == "long":
                        open_pos["stop_disp"] = max(open_pos["stop_disp"], open_pos["entry_px"])
                    else:
                        open_pos["stop_disp"] = min(open_pos["stop_disp"], open_pos["entry_px"])
            stop_disp = open_pos["stop_disp"]

        if open_pos is not None:
            d = 1.0 if open_pos["direction"] == "long" else -1.0
            unreal = d * open_pos["qty"] * (close[i] - open_pos["entry_px"])
            equity_i = cash + unreal
        else:
            equity_i = cash

        if t >= epoch:
            signal_rows.append(dict(
                ts=t, close=float(close[i]),
                upper=_num(upper, i), lower=_num(lower, i), width_rank=_num(width_rank, i),
                sqz_ok=None if sqz_ok is None else bool(sqz_ok[i]),
                long_sig=bool(long_sig[i]), short_sig=bool(short_sig[i]),
                position=open_pos["direction"] if open_pos else None,
                stop_disp=stop_disp, action=action, reason=reason,
            ))
            equity_rows.append((t, equity_i))

    last_ts = int(ts[-1]) if n else None
    state = dict(
        last_ts=last_ts, cash=cash,
        position_dir=open_pos["direction"] if open_pos else None,
        qty=open_pos["qty"] if open_pos else None,
        entry_px=open_pos["entry_px"] if open_pos else None,
        entry_ts=open_pos["entry_ts"] if open_pos else None,
        stop_disp=open_pos["stop_disp"] if open_pos else None,
        equity=equity_rows[-1][1] if equity_rows else cash,
        updated_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    return signal_rows, equity_rows, state


def assert_signal_consistency(symbol, df, sig_df, detail_df):
    """The mandatory each-tick self-proof: compute_signal_detail()'s
    long_sig/short_sig must equal build_signals()'s bar-by-bar, exactly.
    Raises RuntimeError (never continues silently) on the first divergence."""
    mismatch = (sig_df["long_sig"].to_numpy() != detail_df["long_sig"].to_numpy()) | \
               (sig_df["short_sig"].to_numpy() != detail_df["short_sig"].to_numpy())
    if mismatch.any():
        bad_i = int(np.argmax(mismatch))
        raise RuntimeError(
            f"ABORT: {symbol} signal-detail consistency check failed at ts={int(df['ts'].iloc[bad_i])} "
            f"(compute_signal_detail diverges from build_signals) -- refusing to write paper.db")


def health_check(con_market, con_paper, epoch, consistency_results=None):
    """Read-only health check (E2): per symbol, verifies
    (a) last_ts <= now(UTC) - 4h (no unclosed bar has leaked in)
    (b) last_ts is aligned to the 4h grid
    (c) the signal-detail consistency self-proof
    (d) funding-data freshness (E13): newest stored binanceusdm settlement
        must not lag the newest replayed bar by more than one 8h interval
    consistency_results: optional {symbol: bool} from a just-completed
    tick's own assert_signal_consistency calls (avoids recomputation when
    called from run_tick, where a False here would already have aborted the
    whole tick before this point -- see run_tick). When None (e.g. the
    dashboard calling this standalone, with no tick in progress), the check
    is recomputed fresh from current market.db data via a read-only call to
    assert_signal_consistency; a failure there is caught and reported here
    rather than raised, since a health check must never abort."""
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    symbols = {}
    all_ok = True
    for symbol in SYMBOLS:
        reasons = []
        cur = con_paper.execute("SELECT last_ts FROM paper_state WHERE symbol=?", (symbol,))
        row = cur.fetchone()
        last_ts = row[0] if row else None
        if last_ts is None:
            reasons.append("尚無 paper_state 紀錄")
        else:
            if last_ts > now_ms - STEP_MS:
                reasons.append(f"last_ts {iso(last_ts)} 疑為未收盤棒")
            if last_ts % STEP_MS != 0:
                reasons.append(f"last_ts {last_ts} 未對齊 4 小時整點")

        # (d) E13 funding-data freshness: the replay can only settle rates
        # that exist in market.db; if the newest stored settlement
        # (grid-normalized) lags the newest replayed bar by more than one
        # funding interval, flag it -- positions would silently accrue
        # nothing past that point.
        _fn, _ftmin, ftmax = db.funding_stats(con_market, FUNDING_EXCHANGE, funding_symbol(symbol))
        if ftmax is None:
            reasons.append("無 funding 資料")
        elif last_ts is not None and last_ts - grid_ts(ftmax) > FUNDING_MS:
            reasons.append(f"funding 資料落後（最後 {iso(grid_ts(ftmax))}）")

        if consistency_results is not None:
            if consistency_results.get(symbol) is False:
                reasons.append("一致性自證未過")
        else:
            try:
                df = load_df(con_market, symbol, epoch - WARMUP_BARS * STEP_MS)
                if not df.empty:
                    latest_ts = int(df["ts"].max())
                    frame = strategies.build_frame(STRATEGY, df, P, epoch, latest_ts)
                    strategies.check_replay_stability(STRATEGY, df, P, epoch, latest_ts,
                                                      reference=frame)
                    if STRATEGY_NAME == BUILTIN_STRATEGY:
                        assert_signal_consistency(
                            symbol, df, compute_signal_detail(df, epoch, latest_ts, P), frame)
            except RuntimeError as e:      # StrategyError is a RuntimeError
                reasons.append(f"一致性自證未過（{e}）")

        ok = len(reasons) == 0
        all_ok = all_ok and ok
        symbols[symbol] = dict(ok=ok, reasons=reasons, last_ts=last_ts)
    return dict(ok=all_ok, symbols=symbols)


def process_symbol(con_paper, symbol, df, epoch, cash0=CASH0, fee=FEE, tick=None, funding_rates=None,
                   stall_bars=None, stall_gain=None, be_trigger=None):
    """Core per-symbol tick: build signals two ways (canonical + detail),
    abort on any mismatch, run the engine, reconstruct bookkeeping, upsert
    into paper.db. No network access, no market.db writes -- pure given an
    already-loaded OHLCV df. tick=None resolves per-symbol via tick_for()
    (real futures tick sizes for the three traded markets, generic TICK for
    anything else -- the synthetic-series tests' legacy expectations hold
    verbatim); funding_rates flows to engine.run(funding=...) and the
    bookkeeping mirror (None = disabled). Returns a summary dict."""
    if tick is None:
        tick = tick_for(symbol)
    latest_ts = int(df["ts"].max())
    frame = strategies.build_frame(STRATEGY, df, P, epoch, latest_ts)
    # generic self-proof, run for every rule on every tick: the same data
    # twice must give the same answer, and dropping the newest bars must
    # never change the verdict on the older ones (see strategies/__init__).
    strategies.check_replay_stability(STRATEGY, df, P, epoch, latest_ts, reference=frame)
    if STRATEGY_NAME == BUILTIN_STRATEGY:
        # the built-in rule additionally keeps its independent second
        # implementation (compute_signal_detail, written straight from
        # indicators.py's primitives) and refuses to store anything if the
        # two ever disagree on a single bar
        assert_signal_consistency(symbol, df, compute_signal_detail(df, epoch, latest_ts, P), frame)

    metrics, trades_df = engine.run(frame, cash=cash0, fee=fee, tick=tick, funding=funding_rates,
                                    stall_bars=stall_bars, stall_gain=stall_gain,
                                    be_trigger=be_trigger)
    signal_rows, equity_rows, state = build_signal_rows(symbol, frame, trades_df, cash0, fee, epoch,
                                                        funding_rates=funding_rates,
                                                        be_trigger=be_trigger)

    n_trades_before = ps.count_trades(con_paper, symbol)
    n_signals_before = ps.count_signals(con_paper, symbol)

    ps.upsert_trades(con_paper, symbol, trades_df)
    ps.upsert_signals(con_paper, symbol, signal_rows)
    ps.upsert_equity(con_paper, symbol, equity_rows)
    ps.upsert_state(con_paper, symbol, state)

    return dict(
        symbol=symbol, position=state["position_dir"], equity=state["equity"],
        last_ts=state["last_ts"], funding_total=metrics["funding_total"],
        new_trades=ps.count_trades(con_paper, symbol) - n_trades_before,
        new_signal_bars=ps.count_signals(con_paper, symbol) - n_signals_before,
    )


def run_tick():
    print(ANNOUNCEMENT)
    con_market = db.connect()
    fetch_report = refresh_klines(con_market)
    funding_report = refresh_funding(con_market)
    oi_report = refresh_oi(con_market)

    epoch = epoch_ms()
    warmup_start = epoch - WARMUP_BARS * STEP_MS
    con_paper = ps.connect()

    summaries = []
    for symbol in SYMBOLS:
        print(f"[fetch] {symbol}: {fetch_report.get(symbol, 'n/a')}")
        print(f"[funding] {symbol}: {funding_report.get(symbol, 'n/a')}")
        print(f"[oi] {symbol}: {oi_report.get(symbol, 'n/a')}")
        df = load_df(con_market, symbol, warmup_start)
        if df.empty or int(df["ts"].max()) < epoch:
            print(f"[skip] {symbol}: insufficient data through epoch ({PAPER_EPOCH})")
            continue
        try:
            summary = process_symbol(con_paper, symbol, df, epoch,
                                     funding_rates=load_funding(con_market, symbol, epoch),
                                     stall_bars=STALL_BARS, stall_gain=STALL_GAIN,
                                     be_trigger=BE_TRIGGER)
        except RuntimeError as e:
            print(str(e))
            raise
        summaries.append(summary)

    print()
    for s in summaries:
        print(f"{s['symbol']:9s} position={str(s['position']):5s}  equity={s['equity']:10.2f}  "
              f"funding={s['funding_total']:+7.2f}  last_bar={iso(s['last_ts'])}  "
              f"new_trades={s['new_trades']:3d}  new_signal_bars={s['new_signal_bars']:3d}")

    health = health_check(con_market, con_paper, epoch,
                            consistency_results={s["symbol"]: True for s in summaries})
    if health["ok"]:
        print("HEALTH: OK")
    else:
        bad = [f"{sym}({'; '.join(r['reasons'])})" for sym, r in health["symbols"].items() if not r["ok"]]
        print(f"HEALTH: FAIL({' | '.join(bad)})")
    return 0


if __name__ == "__main__":
    sys.exit(run_tick())
