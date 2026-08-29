"""Squeeze Breakout Follow v1 - Python port with Pine-equivalent timing.
Parameters FROZEN to BT-006 defaults. Calibration only; do not tune.

Pine timing replicated:
- process_orders_on_close: market entries/reversals fill at the signal bar's close
  (backtesting.py trade_on_close=True + exclusive_orders=True).
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

import indicators as ind

P = dict(bb_len=20, bb_mult=2.0, rank_len=100, sqz_thresh=20.0, sqz_win=5,
         atr_len=14, stop_mult=2.0)


def build_signals(df, start_ms, end_ms):
    """df: columns ts(ms), Open, High, Low, Close, Volume. Returns copy with
    long_sig / short_sig / atr / in_win columns (Pine-equivalent)."""
    close = df["Close"]
    basis, upper, lower = ind.bollinger(close, P["bb_len"], P["bb_mult"])
    width = ind.bb_width_pct(basis, upper, lower)
    rank = ind.percentrank_prev(width, P["rank_len"])
    sq_recent = rank.rolling(P["sqz_win"]).min().shift(1) < P["sqz_thresh"]
    in_win = (df["ts"] >= start_ms) & (df["ts"] <= end_ms)
    out = df.copy()
    out["long_sig"] = ind.crossover(close, upper) & sq_recent & in_win
    out["short_sig"] = ind.crossunder(close, lower) & sq_recent & in_win
    out["atr"] = ind.atr_rma(df["High"], df["Low"], close, P["atr_len"])
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
