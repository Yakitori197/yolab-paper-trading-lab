"""Squeeze Breakout Follow v1 - legacy entry point, now a thin adapter.

The rule itself moved to strategies/squeeze_breakout.py when the plugin
contract landed; this module stays as the stable import path used by
engine.py (for the default stop_mult), the tests, and the backtesting.py
harness below. P and build_signals() behave exactly as before -- same
formulas, same columns, same values -- they are just sourced from the plugin
now, so there is only ever one implementation of the rule.

Pine timing replicated (unchanged):
- process_orders_on_close: market entries/reversals fill at the signal bar's
  close (backtesting.py trade_on_close=True + exclusive_orders=True).
- Trailing stop: initialized at the signal bar (close - mult*ATR), but the stop
  ORDER only becomes working from the second bar after entry, because Pine's
  strategy.exit is first submitted on the bar after the fill. Replicated by
  setting trade.sl inside next() only while a position exists.
- Ratchet: stop only tightens (max for long / min for short), from close +- ATR.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtesting import Strategy

from strategies import squeeze_breakout as _rule

P = dict(_rule.PARAMS)


def build_signals(df, start_ms, end_ms):
    """df: columns ts(ms), Open, High, Low, Close, Volume. Returns copy with
    long_sig / short_sig / atr / in_win columns (Pine-equivalent).

    Kept to the original four added columns on purpose: backtesting.py's
    Backtest() takes this frame as its data, and the plugin's extra display
    columns (band levels, per-bar reason text) have no business in there.
    """
    raw = _rule.build(df.copy(), P)
    in_win = (df["ts"] >= start_ms) & (df["ts"] <= end_ms)
    out = df.copy()
    out["long_sig"] = raw["long_sig"].to_numpy(dtype=bool) & in_win.to_numpy()
    out["short_sig"] = raw["short_sig"].to_numpy(dtype=bool) & in_win.to_numpy()
    out["atr"] = raw["atr"].to_numpy(dtype=float)
    out["in_win"] = in_win
    return out


class SqueezeBreakout(Strategy):
    def init(self):
        self._stop_l = None
        self._stop_s = None

    def next(self):
        c = float(self.data.Close[-1])
        atr = float(self.data.atr[-1]) if self.data.atr[-1] == self.data.atr[-1] else None
        long_sig = bool(self.data.long_sig[-1])
        short_sig = bool(self.data.short_sig[-1])
        in_win = bool(self.data.in_win[-1])

        if not in_win:
            if self.position:
                self.position.close()
            self._stop_l = self._stop_s = None
            return

        # trailing ratchet (Pine: runs only while a position exists)
        if self.position.is_long and self._stop_l is not None and atr is not None:
            self._stop_l = max(self._stop_l, c - P["stop_mult"] * atr)
            for t in self.trades:
                t.sl = self._stop_l
        elif self.position.is_short and self._stop_s is not None and atr is not None:
            self._stop_s = min(self._stop_s, c + P["stop_mult"] * atr)
            for t in self.trades:
                t.sl = self._stop_s

        if atr is None:
            return

        if long_sig and not self.position.is_long:
            self.buy(size=0.9999)  # exclusive_orders reverses any short at this close
            self._stop_l = c - P["stop_mult"] * atr
            self._stop_s = None
        elif short_sig and not self.position.is_short:
            self.sell(size=0.9999)
            self._stop_s = c + P["stop_mult"] * atr
            self._stop_l = None

        if not self.position and not long_sig and not short_sig:
            self._stop_l = self._stop_s = None
