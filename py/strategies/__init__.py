"""Strategy plugin framework.

A strategy plugin is a single .py file in this directory answering only two
questions per closed bar:

    1. do we go long / short at this bar's close?   -> long_sig / short_sig
    2. how far away does the stop sit?              -> stop_dist (price units)

Everything else stays with the framework and is NOT the plugin's business:
the paper-epoch window, position sizing, fills, fees, slippage, funding
settlement, the trailing/stall/breakeven exits, persistence and the
dashboard. That split is what keeps the dashboard honest -- it renders the
rule the engine actually ran, never a second hand-written copy of it.

Required module attributes:
    NAME    str    human-readable name shown on the dashboard
    PARAMS  dict   default parameters; config.json may override any key
    build(df, params) -> DataFrame

Optional:
    PLOT    dict   overlay declaration, e.g.
                   {"bands": {"upper": "布林上軌", "lower": "布林下軌"},
                    "lines": {"ema_fast": "EMA20"}}
                   -- every declared key must be a column build() returns
    WATCH   dict   what the entry-watch panel measures, e.g.
                   {"label": "距離上/下軌", "score": "watch_score"}

build() receives the full OHLCV frame (columns ts, Open, High, Low, Close,
Volume; ascending, closed bars only) plus the resolved params, and returns a
same-length frame carrying at least long_sig, short_sig and stop_dist. It
must NOT apply the paper-epoch window itself (the framework ANDs it in) and
must NOT modify the OHLCV columns (checked).

The self-proof that replaces per-plugin hand-verification:
check_replay_stability() rebuilds the signals on truncated copies of the same
data and requires the overlapping bars to come out identical. A rule that
peeks at future bars -- shift(-1), rolling(center=True), normalising by the
whole series' max -- changes its own past when new bars arrive and is
rejected there, before anything reaches paper.db.
"""
import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

STRATEGY_DIR = Path(__file__).resolve().parent
PY_DIR = STRATEGY_DIR.parent
if str(PY_DIR) not in sys.path:            # plugins `import indicators` directly
    sys.path.insert(0, str(PY_DIR))

REQUIRED_COLS = ("long_sig", "short_sig", "stop_dist")
OHLCV_COLS = ("ts", "Open", "High", "Low", "Close", "Volume")
NAME_RE = re.compile(r"[A-Za-z0-9_]+")

# cut=0 re-runs build() on the identical frame (determinism); the others drop
# that many bars off the END and require the surviving bars to be unchanged
# (no lookahead). 1 catches next-bar peeking, 3 catches a small window of it.
DEFAULT_CUTS = (0, 1, 3)


class StrategyError(RuntimeError):
    """Raised for any contract violation. Callers abort the tick on this --
    a rule that cannot be trusted must never write to paper.db."""


def available(search_dirs=None):
    """Plugin module names discoverable in the search dirs, sorted."""
    dirs = [Path(d) for d in (search_dirs or [STRATEGY_DIR])]
    names = set()
    for d in dirs:
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.py") if not p.stem.startswith("_"))
    return sorted(names)


def load(name, search_dirs=None):
    """Import the plugin module `name` from the first search dir holding it.
    Loaded by file path (not the import system) so a plugin dropped in by the
    user needs no packaging and cannot shadow a stdlib module."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise StrategyError(f"策略名稱只能用英數字與底線,收到:{name!r}")
    dirs = [Path(d) for d in (search_dirs or [STRATEGY_DIR])]
    for d in dirs:
        path = d / f"{name}.py"
        if path.is_file():
            return _load_path(name, path)
    looked = " / ".join(str(d) for d in dirs)
    raise StrategyError(
        f"找不到策略 {name!r}(找過:{looked})。可用的策略:{', '.join(available(dirs)) or '(無)'}")


def _load_path(name, path):
    spec = importlib.util.spec_from_file_location(f"_strategy_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                 # a broken plugin must not look like a framework bug
        raise StrategyError(f"策略 {name!r} 匯入失敗({type(e).__name__}: {e})") from e
    mod.__strategy_name__ = name
    _validate_module(name, mod)
    return mod


def _validate_module(name, mod):
    if not callable(getattr(mod, "build", None)):
        raise StrategyError(f"策略 {name!r} 缺少 build(df, params) 函式")
    if not isinstance(getattr(mod, "PARAMS", None), dict):
        raise StrategyError(f"策略 {name!r} 缺少 PARAMS 參數字典")
    if not isinstance(getattr(mod, "NAME", None), str) or not mod.NAME.strip():
        raise StrategyError(f"策略 {name!r} 缺少 NAME 顯示名稱")
    for attr in ("PLOT", "WATCH"):
        val = getattr(mod, attr, None)
        if val is not None and not isinstance(val, dict):
            raise StrategyError(f"策略 {name!r} 的 {attr} 必須是 dict 或不定義")


def resolve_params(mod, overrides=None):
    """Defaults from the plugin, overridden by config.json. An unknown key is
    an error rather than a silent no-op -- a typo'd parameter that quietly
    does nothing is exactly the kind of thing this project refuses to do."""
    params = dict(mod.PARAMS)
    for key, val in dict(overrides or {}).items():
        if key not in params:
            raise StrategyError(
                f"策略 {getattr(mod, '__strategy_name__', '?')} 沒有參數 {key!r};"
                f"可用參數:{', '.join(sorted(params)) or '(無)'}")
        params[key] = val
    return params


def plot_spec(mod):
    """Normalized {'bands': {col: label}, 'lines': {col: label}}."""
    raw = getattr(mod, "PLOT", None) or {}
    out = {}
    for kind in ("bands", "lines"):
        block = raw.get(kind) or {}
        if block:
            out[kind] = {str(k): str(v) for k, v in block.items()}
    return out


def describe(mod, params=None):
    """Serializable summary for /api/summary and the dashboard."""
    return dict(
        module=getattr(mod, "__strategy_name__", None),
        name=getattr(mod, "NAME", None),
        params=dict(params if params is not None else mod.PARAMS),
        plot=plot_spec(mod),
        watch=dict(getattr(mod, "WATCH", None) or {}),
    )


def declared_columns(mod):
    """Extra columns the dashboard should persist for this plugin: whatever
    PLOT/WATCH declares, plus the optional per-bar `reason` text."""
    cols = []
    for block in plot_spec(mod).values():
        cols.extend(block)
    watch = getattr(mod, "WATCH", None) or {}
    for key in ("score", "text"):
        if watch.get(key):
            cols.append(str(watch[key]))
    if "reason" not in cols:
        cols.append("reason")
    seen, out = set(), []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def build_frame(mod, df, params, start_ms, end_ms):
    """Run the plugin and return an engine-ready frame.

    The returned frame is the input OHLCV plus every column the plugin
    produced, with the framework's own paper-epoch window ANDed into the
    signals (`in_win`). The plugin never sees or applies the window itself,
    so it cannot accidentally trade the warmup bars.
    """
    name = getattr(mod, "__strategy_name__", getattr(mod, "NAME", "?"))
    try:
        out = mod.build(df.copy(), dict(params))
    except Exception as e:
        raise StrategyError(f"策略 {name} 的 build() 執行失敗({type(e).__name__}: {e})") from e
    if not isinstance(out, pd.DataFrame):
        raise StrategyError(f"策略 {name} 的 build() 必須回傳 DataFrame,收到 {type(out).__name__}")
    if len(out) != len(df):
        raise StrategyError(
            f"策略 {name} 的 build() 回傳 {len(out)} 列,與輸入的 {len(df)} 根 K 棒不符;"
            "請保持逐棒對齊,不要刪除暖機期的列(不足的地方填 NaN/False 即可)")
    missing = [c for c in REQUIRED_COLS if c not in out.columns]
    if missing:
        raise StrategyError(f"策略 {name} 的 build() 缺少必要欄位:{', '.join(missing)}")

    # the plugin may not rewrite price history -- an indicator that "fixes"
    # the input would invalidate every fill the engine computes from it
    for col in OHLCV_COLS:
        if col in out.columns and col in df.columns:
            a, b = out[col].to_numpy(), df[col].to_numpy()
            if not np.array_equal(a, b, equal_nan=True):
                raise StrategyError(f"策略 {name} 的 build() 改動了 {col} 欄;OHLCV 必須原封不動")

    frame = df.copy()
    for col in out.columns:
        if col not in OHLCV_COLS:
            frame[col] = out[col].to_numpy()

    for col in ("long_sig", "short_sig"):
        try:
            frame[col] = frame[col].astype(bool)
        except (TypeError, ValueError) as e:
            raise StrategyError(f"策略 {name} 的 {col} 必須能轉成 True/False({e})") from e
    try:
        frame["stop_dist"] = frame["stop_dist"].astype(float)
    except (TypeError, ValueError) as e:
        raise StrategyError(f"策略 {name} 的 stop_dist 必須是數值({e})") from e
    neg = frame["stop_dist"].to_numpy()
    if np.any(neg < 0):
        bad = int(np.argmax(neg < 0))
        raise StrategyError(
            f"策略 {name} 在 ts={int(frame['ts'].iloc[bad])} 給出負的 stop_dist "
            f"({neg[bad]});停損距離是「離進場價多遠」,恆為正數")

    for col, label in ((c, l) for block in plot_spec(mod).values() for c, l in block.items()):
        if col not in frame.columns:
            raise StrategyError(
                f"策略 {name} 的 PLOT 宣告了「{label}」({col}),但 build() 沒有回傳這一欄")

    in_win = (frame["ts"] >= start_ms) & (frame["ts"] <= end_ms)
    frame["in_win"] = in_win
    frame["long_sig"] = frame["long_sig"] & in_win
    frame["short_sig"] = frame["short_sig"] & in_win
    if "atr" not in frame.columns:          # engine only needs it for the legacy path
        frame["atr"] = frame["stop_dist"]
    return frame


def check_replay_stability(mod, df, params, start_ms, end_ms, cuts=DEFAULT_CUTS, reference=None):
    """The generic self-proof, run every tick before anything is stored.

    Rebuilds the signals with the last `cut` bars removed and requires every
    surviving bar to be identical to the full-length run. Two failure modes
    are caught:
      cut = 0  -> the rule is not deterministic (same input, different output)
      cut > 0  -> the rule looks ahead: its verdict on a past bar depends on
                  bars that had not closed yet, so today's history silently
                  rewrites yesterday's -- and a backtest of it flatters
                  itself with information no live trader had.
    Raises StrategyError naming the first divergent bar; returns the number
    of comparisons actually performed.
    """
    name = getattr(mod, "__strategy_name__", getattr(mod, "NAME", "?"))
    full = reference if reference is not None else build_frame(mod, df, params, start_ms, end_ms)
    n = len(df)
    checked = 0
    for cut in cuts:
        keep = n - cut
        if keep < 2:
            continue
        part = build_frame(mod, df.iloc[:keep].copy(), params, start_ms, end_ms)
        for col in ("long_sig", "short_sig"):
            a = full[col].to_numpy()[:keep]
            b = part[col].to_numpy()
            if not np.array_equal(a, b):
                _raise_divergence(name, full, cut, int(np.argmax(a != b)), col)
        a = full["stop_dist"].to_numpy()[:keep]
        b = part["stop_dist"].to_numpy()
        same = np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True)
        if not same.all():
            _raise_divergence(name, full, cut, int(np.argmax(~same)), "stop_dist")
        checked += 1
    return checked


def _raise_divergence(name, full, cut, i, col):
    ts = int(full["ts"].iloc[i])
    if cut == 0:
        raise StrategyError(
            f"ABORT: 策略 {name} 不具決定性 -- 相同資料連跑兩次,ts={ts} 的 {col} 就不一樣了。"
            "build() 內不可使用隨機數、目前時間或會被前一次呼叫改動的全域狀態。")
    raise StrategyError(
        f"ABORT: 策略 {name} 用到了未來資料 -- 把最後 {cut} 根 K 棒拿掉重算後,"
        f"ts={ts} 的 {col} 就變了。這代表它對某一根棒的判斷取決於當時還沒收盤的資料,"
        "回測會虛胖而實盤重現不了。常見成因:shift(-1)、rolling(center=True)、"
        "用整段序列的最大/最小值做正規化。")
