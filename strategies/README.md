# 🗂 策略清單總表 — Strategy Inventory

> 最後更新：2025-07-10
> 目的：為後續標準化、版本管理、參數統整提供基礎架構

---

## 0. 標準化模板 — Strategy Template

| 欄位 | 內容 |
|------|------|
| **檔案** | `_TEMPLATE.pine` |
| **使用指南** | `_TEMPLATE_README.md` |
| **Pine 版本** | v5 |
| **用途** | 所有新策略的開發起點，統一信號輸出格式、參數命名規範和註釋風格 |
| **狀態** | ✅ 已定稿 |

### 模板規範摘要

| 規範 | 說明 |
|------|------|
| **信號輸出** | 標準化 9 類信號：`longE/shortE`（入場）、`longX/shortX`（離場）、`longSL/shortSL`（止損）、`longTP1-3/shortTP1-3`（三層止盈） |
| **命名前綴** | `i_`=輸入參數、`G_`=分組、`c_`=顏色、`f_`=函數、`p_`=繪圖 |
| **止盈止損** | 三種模式：Trailing（追蹤）、ATR（動態三層 TP+SL）、Options（僅開倉） |
| **風險管理** | 內建 ATR 動態計算、可調倍數、三層分批止盈 |
| **視覺化** | 統一的 K 線著色、信號標籤、TP/SL 價格線 |
| **儀表盤** | 回測績效表格（勝率、收益率、利潤因子、最大回撤） |
| **警報整合** | Webhook 信號輸出，支援外部交易系統接入 |

詳見 [`_TEMPLATE_README.md`](_TEMPLATE_README.md)。

---

## 1. LE VAN DO® — Swing Signals & Overlays Private™

| 欄位 | 內容 |
|------|------|
| **版本** | 7.9-X (2024.3.20) |
| **Pine 版本** | v5 |
| **原始碼檔案** | `LE_VAN_DO_Swing_Signals_7.9-X.pine` |
| **參數定義** | `LE_VAN_DO_Swing_Signals_7.9-X/params.json` |
| **版本記錄** | `LE_VAN_DO_Swing_Signals_7.9-X/version.json` |
| **策略類型** | 動能反轉混合策略 |
| **交易對** | 未指定（回測範圍 fromDate 設定 2023年起） |
| **關聯交易所** | 未指定（策略無交易所繫結） |

### 進出場邏輯

| 模式 | 進場條件 | 出場條件 |
|------|---------|---------|
| **Open/Close** | Heikin Ashi K線收盤價突破開盤價：`crossover(closeSeriesAlt, openSeriesAlt)` | 反向訊號平倉（Trailing模式）；或三層 TP + SL（ATR模式） |
| **Renko** | Renko 收盤價 EMA 快線(2) 突破慢線(10)：`crossover(a, b)` | 同上 |

### 止盈止損模式

| 模式 | 說明 |
|------|------|
| **Trailing** | 反向訊號平倉（無固定 TP/SL） |
| **ATR** | 三層 TP（ATR×2.5 / ATR×5.0 / ATR×7.5）+ ATR 止損 |
| **Options** | 僅進場不平倉（需手動或外部系統管理） |

### 風險參數

| 參數 | 值 |
|------|-----|
| 初始資金 | $5,000 |
| 倉位比例 | 權益的 50% |
| 手續費 | 0.02% |
| TP1 倉位比例 | 50% |
| TP2 倉位比例 | 30% |
| TP3 倉位比例 | 20% |

### 橫盤過濾（7 種模式）

1. Filter with ATR
2. Filter with RSI
3. ATR or RSI
4. ATR and RSI
5. No Filtering
6. Entry Only in sideways market (by ATR or RSI)
7. Entry Only in sideways market (by ATR and RSI)

### 配置參數一覽

| 分類 | 參數數量 | 用戶可調 | 程式碼硬編碼 |
|------|---------|---------|------------|
| 進場模式 | 2 | 2 | 0 |
| 顯示設定 | 2 | 2 | 0 |
| 回測範圍 | 3 | 3 | 0 |
| 橫盤過濾 | 1 | 1 | 0 |
| RSI 過濾 | 3 | 3 | 0 |
| ATR 過濾 | 2 | 0 | 2 |
| Renko 設定 | 4 | 2 | 2 |
| 風險管理 | 7 | 3 | 4 |
| 內部時間框架 | 1 | 0 | 1 |
| Webhook 訊息 | 12 | 0 | 12 |
| 儀表板 | 3 | 3 | 0 |
| **合計** | **40** | **19** | **21** |

### 附屬模組（原始碼內建）

- ZigZag 市場結構（由 Spec 提及，當前原始碼中未見實作 → 歸類為 spec_documented_only）
- Order Blocks（同上）
- FVG 失衡區（同上）
- EMA 雲（同上）
- DEMA ATR（同上）
- 策略績效儀表板 ✓
- 週度績效儀表板 ✓
- 月度績效儀表板 ✓
- Webhook 警報 ✓

---

## 2-9. 待補充策略

> 以下策略待後續加入。目前僅有 LE VAN DO® 一支策略納管。

### 填入模板

```json
{
  "name": "策略名稱",
  "version": "X.Y.Z",
  "file": "strategies/策略文件名.pine",
  "params": "strategies/策略文件名/params.json",
  "version_record": "strategies/策略文件名/version.json",
  "exchange": "交易所名稱",
  "pair": "交易對",
  "type": "趨勢/反轉/網格/...",
  "status": "回測中/實盤中/已棄用"
}
```

---

## 目錄結構說明

```
strategies/
├── README.md                                    ← 策略清單總表（本檔案）
├── _TEMPLATE.pine                               ← 策略開發模板（新策略由此複製）
├── _TEMPLATE_README.md                          ← 模板使用指南
├── LE_VAN_DO_Swing_Signals_7.9-X.pine           ← 原始原始碼
├── LE_VAN_DO_Swing_Signals_7.9-X/
│   ├── params.json                              ← 結構化參數定義
│   └── version.json                             ← 版本變更記錄
```

### 命名規範

- 原始碼檔案：`<策略名稱>_<版本>.pine`
- 參數資料夾：`<策略名稱>_<版本>/`
- 參數 JSON：`params.json`
- 版本記錄：`version.json`

### 使用模板開發新策略

1. 複製模板：`cp _TEMPLATE.pine <新策略名稱>_<版本>.pine`
2. 閱讀 [`_TEMPLATE_README.md`](_TEMPLATE_README.md) 了解各區域的修改指引
3. 修改策略名稱、版本號
4. 在「信號計算區」實現核心交易邏輯，輸出 `leTrigger` / `seTrigger`
5. 按需調整風險參數和視覺化設定
6. 建立同名資料夾存放 `params.json` 和 `version.json`
7. 更新本清單

### 新建策略步驟（傳統方式）

1. 將 `.pine` 原始碼放入 `strategies/` 根目錄
2. 建立同名資料夾（不含 `.pine` 副檔名）
3. 在資料夾內建立 `params.json` 和 `version.json`
4. 更新本 `README.md` 策略清單
