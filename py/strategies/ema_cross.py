"""EMA 交叉 — 第二個範例策略,存在的目的是「證明外掛介面不是為布林擠壓量身訂做的」。

規則本身刻意寫得最簡單:快線上穿慢線做多、下穿做空,停損放在 stop_mult 倍
ATR 之外。它同樣不是一條有邊際的規則,只是一份可以照抄的骨架。

把這支檔案複製成 strategies/你的規則.py,改掉 build() 裡面的算法,再到
config.json 把 strategy.module 指向新檔名,就換完了。完整契約見
docs/STRATEGY_API.md。
"""
import numpy as np

import indicators as ind

# 顯示在儀表板上的名字
NAME = "EMA 交叉（範例）"

# 預設參數。config.json 的 strategy.params 可以覆寫任何一個 key;
# 打錯 key 會直接報錯,不會安靜地當作沒看到。
PARAMS = dict(fast=20, slow=60, atr_len=14, stop_mult=2.5)

# 要畫在主圖上的線:欄位名 -> 顯示名稱。宣告了就必須在 build() 裡回傳同名欄位。
PLOT = dict(lines={"ema_fast": "EMA 快線", "ema_slow": "EMA 慢線"})

# 進場監視面板要顯示的東西(text 指向一個逐棒的文字欄位)
WATCH = dict(label="快慢線距離", text="watch_text")


def build(df, params):
    """df:ts / Open / High / Low / Close / Volume,由舊到新、全是已收盤的棒。

    回傳同樣長度的 DataFrame,至少要有三個欄位:
        long_sig   這根收盤要不要做多
        short_sig  這根收盤要不要做空
        stop_dist  停損離進場價多遠(價格單位,已經乘好倍數)

    三個不要做的事:
        1. 不要自己套用起算日窗口 —— 框架會處理(暖機棒永遠不會下單)
        2. 不要改動 OHLCV 欄位 —— 會被擋下來
        3. 不要看未來 —— 用到 shift(-1)、rolling(center=True)、
           或整段序列的最大值,前瞻檢查會在寫入資料庫前抓到並中止
    """
    close = df["Close"]
    fast = close.ewm(span=params["fast"], adjust=False).mean()
    slow = close.ewm(span=params["slow"], adjust=False).mean()
    atr = ind.atr_rma(df["High"], df["Low"], close, params["atr_len"])

    cross_up = ind.crossover(fast, slow)
    cross_dn = ind.crossunder(fast, slow)

    # ewm(adjust=False) 從第一根就有值,但前面那幾十根其實還沒「長成」均線。
    # 慢線長度以內的棒一律不出訊號,免得拿暖機期的雜訊當交叉。
    warm = np.arange(len(df)) >= params["slow"]

    out = df.copy()
    out["ema_fast"] = fast
    out["ema_slow"] = slow
    out["long_sig"] = cross_up & warm
    out["short_sig"] = cross_dn & warm
    out["atr"] = atr
    out["stop_dist"] = params["stop_mult"] * atr
    out["reason"] = [None if (u or d) else "快線尚未穿越慢線"
                     for u, d in zip(out["long_sig"], out["short_sig"])]
    gap = (fast - slow) / close * 100.0
    out["watch_text"] = [None if g != g else f"快線距慢線 {g:+.2f}%" for g in gap]
    return out
