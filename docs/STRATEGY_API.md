# 策略外掛介面

換上自己的規則,只需要寫一支 Python 檔放進 `py/strategies/`,再到 `config.json`
指名它。引擎、成本模型、出場管理、帳本、儀表板都不用動。

一句話版本:**你的規則只回答「這根收盤要不要進場」和「停損放多遠」,其餘一律不歸你管。**

---

## 最小可用範例

存成 `py/strategies/my_rule.py`:

```python
import indicators as ind

NAME = "我的規則"
PARAMS = dict(fast=20, slow=60, atr_len=14, stop_mult=2.5)

def build(df, params):
    close = df["Close"]
    fast = close.ewm(span=params["fast"], adjust=False).mean()
    slow = close.ewm(span=params["slow"], adjust=False).mean()
    atr = ind.atr_rma(df["High"], df["Low"], close, params["atr_len"])

    out = df.copy()
    out["long_sig"] = ind.crossover(fast, slow)
    out["short_sig"] = ind.crossunder(fast, slow)
    out["stop_dist"] = params["stop_mult"] * atr
    return out
```

`config.json`:

```json
"strategy": { "module": "my_rule", "params": { "fast": 12 } }
```

刪掉 `data/paper.db`,重跑一次 tick,整本帳就是你的規則跑出來的了。

完整可讀的範例看 [`py/strategies/ema_cross.py`](../py/strategies/ema_cross.py)(逐行中文註解),
內建規則看 [`py/strategies/squeeze_breakout.py`](../py/strategies/squeeze_breakout.py)。

---

## 契約

### 模組層級

| 名稱 | 必要 | 說明 |
|---|---|---|
| `NAME` | ✅ | 顯示在儀表板上的規則名稱 |
| `PARAMS` | ✅ | 預設參數 dict。`config.json` 的 `strategy.params` 可覆寫任何一個 key;**打錯 key 會直接報錯**,不會靜默忽略 |
| `build(df, params)` | ✅ | 見下 |
| `PLOT` | | 要畫在主圖上的線:`{"bands": {欄位: 顯示名}, "lines": {...}}`。宣告了就必須回傳同名欄位 |
| `WATCH` | | 進場監視面板要看什麼:`{"label": "...", "text": "欄位名"}` |

### `build(df, params)`

**收到**:`ts / Open / High / Low / Close / Volume` 的 DataFrame,由舊到新,**全部是已收盤的棒**
(不會有還在跳動的那根)。`ts` 是毫秒 UTC。

**回傳**:同樣長度的 DataFrame,至少含這三欄:

| 欄位 | 型別 | 意義 |
|---|---|---|
| `long_sig` | bool | 這根收盤價要做多 |
| `short_sig` | bool | 這根收盤價要做空 |
| `stop_dist` | float | 停損離進場價多遠(價格單位,**已經乘好倍數**);NaN = 這根不足以進場 |

選用欄位:

| 欄位 | 用途 |
|---|---|
| `reason` | 這根「為什麼沒進場」的說明,會顯示在策略日誌;條件都成立時填 `None` |
| `PLOT` / `WATCH` 宣告的欄位 | 給圖表與監視面板用 |
| `atr` | 有就存,沒有也沒關係 |

**三件不要做的事**:

1. **不要自己套用起算日窗口** — 框架會 AND 進去,暖機棒永遠不會下單
2. **不要改動 OHLCV 欄位** — 會被擋下來(引擎的成交價全部建立在這些數字上)
3. **不要看未來** — 見下一節

### 分工線

| 你的規則負責 | 框架負責(不用碰) |
|---|---|
| 什麼時候進場 | 起算日窗口、部位大小、成交價 |
| 停損距離多遠 | 手續費、滑價、資金費率結算 |
| 要畫哪些線、監視什麼 | 追蹤停損、停滯出場、保本鎖 |
| 沒進場時說什麼理由 | 寫入 paper.db、儀表板呈現、健康檢查 |

出場規則是框架層的,在 `config.json` 的 `exits` 區調整(設成 `null` 即停用):

```json
"exits": { "stall_bars": 6, "stall_gain": 0.0, "be_trigger": 1.0 }
```

---

## 系統會怎麼檢查你的規則

每次 tick、寫進資料庫**之前**跑,任何一項不過就中止,帳本一個字都不會動。

### 1. 決定性

同一份資料連跑兩次,結果必須完全一樣。用到隨機數、現在時間、或會被上一次呼叫改掉的
全域變數,就會被抓到。

### 2. 無前瞻(這條最重要)

把最後 1 根、3 根 K 棒拿掉重算,**前面那些棒的訊號必須一模一樣**。

不一樣,就代表你的規則對某一根棒的判斷取決於當時還沒收盤的資料——今天的新資料會偷偷
改寫昨天的歷史。這種規則的回測一定漂亮,實盤一定重現不了。常見成因:

- `shift(-1)` 或任何取「下一根」的動作
- `rolling(center=True)`
- 用整段序列的最大/最小值做正規化(`close / close.max()`)
- 先算完整段再回填

錯誤訊息會直接告訴你是哪一根棒(ts)開始不一致。

### 3. 契約檢查

回傳列數對不對、必要欄位在不在、有沒有改到 OHLCV、`stop_dist` 是不是負的、
`PLOT` 宣告的欄位有沒有真的回傳。

> 內建的擠壓規則另外還有一層:`paper_loop.compute_signal_detail()` 用
> `indicators.py` 的基本函式把同一條規則**獨立再寫一遍**,兩份逐棒比對,不一致就中止。
> 這一層不會要求你也做——沒有人應該把自己的規則寫兩遍——你的規則靠上面三項把關。

---

## 儀表板會跟著變什麼

- **規則標籤 / 橫幅**:顯示你的 `NAME`
- **策略日誌**:顯示你的 `reason` 文字
- **追蹤停損卡**:距離取自你的 `stop_dist`
- **進場監視**:內建擠壓規則專屬的閘門數字(寬度排名、上下軌距離)在別的規則下**不顯示**,
  面板會註明目前跑的是哪條規則。要讓它顯示你自己的東西,是下一階段的工作。
- **主圖軌道**:目前只畫內建規則的布林軌道;`PLOT` 宣告的線尚未接到圖表上。

換句話說:現在換規則,帳目與紀錄是完全正確的,圖表與監視面板則會「少畫東西」而不是
「畫錯東西」。少畫是誠實,畫錯不是。

---

## v1 的限制(刻意的)

- 單一部位、全額進出,反向訊號直接反手;不支援加碼、不支援同時持有多個部位
- 週期固定 4H(資金費率結算網格建立在 4H 上)
- 只在**收盤**決策,不支援盤中觸發
- 每個商品各自一本獨立的帳,彼此不撥款

---

## 除錯

跑一次測試,確認框架與你的檔案都還健康:

```bash
py\.venv\Scripts\python -m pytest py\tests -q
```

只想確認自己的規則過不過三項檢查,不必等 tick:

```python
import sys; sys.path.insert(0, "py")
import pandas as pd, sqlite3, strategies, db, paper_loop as pl

mod = strategies.load("my_rule")
df = pl.load_df(db.connect(), "BTC/USDT", 0)        # 需已跑過至少一次 tick
strategies.check_replay_stability(mod, df, mod.PARAMS, pl.epoch_ms(), int(df["ts"].max()))
print("通過")
```
