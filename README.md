# gazeweb — Tobii 眼動儀 × 瀏覽器 視線錄製／回放／匯出

接上 Tobii 眼動儀，開一個瀏覽器讓你正常上網；結束後產生：

- **replay.html**：像影片一樣回放整個瀏覽過程（DOM 由 rrweb 重建），並疊上視線點、軌跡、注視點
- **整頁視線圖**：每個造訪過的網址拍整頁截圖，畫上熱點圖或編號掃視路徑，可播放視線移動
- **gaze.csv / gaze.json**：每個時間點的座標（Tobii 原始正規化值、螢幕像素、網頁可視座標、整頁座標、捲動量、網址）
- **fixations.csv**：I-DT 演算法偵測出的注視點（位置、開始時間、停留時間）

Tobii 這邊的運作方式與逆向證據見 [docs/tobii-ghost-analysis.md](docs/tobii-ghost-analysis.md)。

## 安裝

需要 Windows、Python 3.10+（64-bit）、已安裝的 Chrome 或 Edge。攝影機模式第一次執行會自動下載 MediaPipe 臉部模型（約 3.6 MB，存到 `%LOCALAPPDATA%\gazeweb\models`）。

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Tobii 端需要：眼動儀接上、**Tobii Experience** 在執行並完成顯示器設定與校正。
`tobii_stream_engine.dll` 會自動從 Tobii Ghost 的安裝資料夾找；也可以用 `--dll` 或環境變數 `TOBII_STREAM_ENGINE_DLL` 指定。

## 使用

```bash
.venv\Scripts\python -m gazeweb devices
```

```bash
.venv\Scripts\python -m gazeweb record https://www.example.com
```

在打開的瀏覽器裡正常瀏覽（可以點連結、捲動、開新分頁）。**關閉瀏覽器視窗或在終端機按 Ctrl+C** 結束。之後會在背景用同一個瀏覽器設定檔重開造訪過的網址拍整頁截圖，再把結果寫到 `sessions\<時間>\`，打開裡面的 `replay.html` 就能看。

### 沒有 Tobii：用電腦攝影機代替

```bash
.venv\Scripts\python -m gazeweb calibrate
```

先單獨跑一次校正，確認精度：

1. **位置調整**：畫面顯示鏡頭影像與提示（太遠、太近、偏移），位置合適時按空白鍵。
2. **校正**：紅點依序移到 13 個位置（`--points 9` 可減少），盯著圓點中心即可，約 30 秒。
3. **驗證**：另外 5 個點量測實際誤差，顯示像素、公分、視角度數與評估；Enter 接受、R 重做。
4. **即時測試**：全螢幕顯示估計的視線點，看看格線圓圈，Esc 結束。

確認可以之後錄製：

```bash
.venv\Scripts\python -m gazeweb record https://www.example.com --source webcam
```

錄製前一定會先定位，兩種修正並行：

- **開始時定位**：30 分鐘內校正過，就只看 5 個點（約 10 秒）量出目前坐姿造成的偏差，當作初始偏移；更舊、或偏差太大時做完整校正。`--recalibrate` 可強制完整校正。
- **錄製中點擊**：每次點擊都當作參考點（人點擊時通常正看著點擊位置），和開始時的定位點一起估計偏移（定位點權重 2 倍、舊的參考點隨時間淡出、明顯沒看著點擊處的會捨棄）。不想要可加 `--no-click-calibration`。

模擬測試：坐姿改變後不修正約 310 px；只靠點擊需要幾次點擊才降到約 80 px；定位＋點擊並行一開始就約 60 px，6 次點擊後約 50 px。

原理：MediaPipe Face Landmarker 取得 478 個臉部點（含虹膜）與頭部姿態 → 每眼虹膜相對眼角的位置、眼瞼開合、頭部朝向與位置組成特徵 → 校正資料以脊迴歸擬合（正則化強度用留一點交叉驗證自動選）→ One Euro 濾波平滑；眨眼或偵測不到臉時樣本標為無效。

精度期待要放低：一般筆電鏡頭大約 **2–5° 視角**（60 cm 距離約 2–5 cm，等於幾行文字的高度），Tobii 約 0.5–1°。適合看「看了哪個區塊」，不適合分析「讀到哪個字」。提升精度的方法：光線從正面來、避免背光、校正後維持坐姿、頭盡量不動、多點擊。

### 滑鼠模擬

沒有眼動儀時，也可以用滑鼠模擬視線，測試整個流程：

```bash
.venv\Scripts\python -m gazeweb record https://www.example.com --source mouse
```

常用選項：

| 選項 | 說明 |
|---|---|
| `--source tobii\|webcam\|mouse` | 視線來源（預設 tobii） |
| `--camera N` / `--points 9\|13` / `--distance cm` | 攝影機編號、校正點數、眼睛到螢幕距離（換算視角誤差用） |
| `--monitor N` | 眼動儀裝在哪個螢幕（預設主螢幕；編號見 `devices`） |
| `--browser auto\|chrome\|msedge\|chromium` | 使用的瀏覽器 |
| `--duration 秒` | 錄到指定秒數自動停止 |
| `--no-screenshots` | 不拍整頁截圖 |
| `--field-of-use analytical\|interactive` | 向 Stream Engine 宣告的用途（見下方授權說明） |
| `--out 資料夾` | 輸出位置（預設 `./sessions`） |

已錄好的資料夾可以重新產生 CSV / 注視點 / 回放頁：

```bash
.venv\Scripts\python -m gazeweb rebuild sessions\20261018-120000
```

## 座標怎麼算

```
Tobii gaze point (nx, ny) ∈ [0,1]            ← tobii_gaze_point_subscribe
  × 顯示器矩形                → 螢幕實體像素 (screen_x, screen_y)
  − 瀏覽器內容區 HWND 左上角
  ÷ devicePixelRatio           → 可視座標 (viewport_x, viewport_y) = clientX/Y
  + scrollX / scrollY          → 整頁座標 (page_x, page_y) = pageX/Y
```

- 內容區位置取自前景視窗的 `Chrome_RenderWidgetHostHWND`，並和分頁回報的 `innerWidth × devicePixelRatio` 比對大小，確認是同一個視窗（`mapping = hwnd`）。比對不上時，退回用 JS 的 `screenX/outerHeight` 估計（`mapping = estimated`，頁面縮放不是 100% 時會不準）。
- 時間戳：Tobii 裝置時鐘（µs）用 `tobii_system_clock` 和系統時間對時（和 Tobii Ghost 用的方法一樣），換成 Unix epoch 毫秒，和 rrweb 事件放在同一條時間軸上。
- 只錄「目前聚焦的分頁」；切換分頁時會重新拍 DOM 快照，所以回放會跟著切。

## 回放頁（replay.html）

剪輯台式的版面：左邊是網站回放，右邊是整頁視線圖，下方是多軌時間軸（段落、注視點、點擊、捲動、水平視線），共用一條播放頭。

- **調整比例**：拖曳回放與視線圖中間的分隔線（或選取分隔線後按 ← →，雙擊恢復預設），比例會記住。
- **視線圖圖層**：熱點、路徑（注視順序）、滑鼠位置、點擊、標註時間，可各自開關；背景可選完整長截圖或錄製當下畫面。播放頭之後的資料會淡化，白框是當下的可視範圍。
- **時間軸縮放**：播放列的「時間軸縮放」滑桿（1× 到約 1 秒佔滿整條），或在時間軸上用滾輪縮放（以滑鼠位置為中心）；Shift＋滾輪、觸控板橫向滑動或下方捲軸可左右移動；鍵盤 ＋／－ 縮放、0 或「全部」回到整段。尺規刻度會依縮放自動調整（5 分鐘到 0.1 秒，含次刻度），播放或跳轉時可視範圍會跟著播放頭。
- **時間軸**：點任何一列都會把播放頭移到點的時間。在最上方尺規上拖曳可設定 **A–B 循環播放** 區段（立刻開始播放，到結尾自動回到開頭），按「停止循環」或 Esc 取消。
- **匯出**：右上角「匯出」，選擇要哪些段落或輸入時間範圍，勾選內容（視線樣本、注視點、點擊、滑鼠軌跡、段落表），下載成一個 ZIP（CSV 皆為 UTF-8 含 BOM，Excel 可直接開）。
- **風格**：右上角選單切換五套風格（剪輯台・石墨、調色室・深藍、淺色實驗室、高對比、研究報告），瀏覽器會記住；網址加 `?theme=lab` 等也可以指定。比較頁：`design-options/themes.html`。
- **跳到下一個時間點**：時間軸「段落、注視點、點擊」三列右側各有 ◀ ▶ 與「目前／總數」，◀ 一定到前一個、▶ 一定到下一個（數字每按一次減一或加一），視線圖會捲到那個注視點或點擊。
- **語言**：右上角「語言 / Language」切換中文、English（介面、匯出對話框與 ZIP 內的 README 都會跟著換），瀏覽器會記住；網址加 `?lang=en` 也可以指定。
- 鍵盤：空白鍵播放／暫停，← → 前後段落，Esc 取消選取。

舊的錄製用 `rebuild` 重建就會換成新版回放頁。

## 整頁視線圖：以「造訪段落」切換

錄製會依時間切成**造訪段落**：連續停在同一分頁、同一頁面算一段；換網址或切換分頁就開新段落（只改 `#…` 不算換頁；之後回到同一網址也是新的一段）。

- **◀ ▶**（或鍵盤 ← →）切換前後段落，上方時間軸顯示所有段落的實際時間位置（顏色＝分頁），可直接點選。
- 每段標示時間範圍：距錄製開始的時間與時鐘時間，例如 `0:07.4 – 0:10.8（3.4 秒）· 06:39:09 – 06:39:12`。
- 掃視路徑的每個注視點標註「開始時間 · 停留毫秒」（可用「標註時間」開關）。
- 「在回放中查看」跳到網站回放的該段落開頭。
- `visits.csv` 列出所有段落；`gaze.csv`、`fixations.csv` 的 `visit` 欄位對應段落編號。

## 頁面內捲動的網站

HackMD、Notion、Gmail 這類網站捲動的是頁面裡的一個大區塊，整個視窗本身不捲動（`window.scrollY` 一直是 0）。錄製時會自動偵測「覆蓋可視區一半以上、可捲動」的主要容器，把它的捲動量算進 `scroll_x/scroll_y`、整頁座標與截圖位置。

舊版錄下的檔案用 `rebuild` 重建時，會用 rrweb 記錄的元素捲動事件自動補正（`meta.json` 的 `scroll_model` 為 `inner-scroll-fixed`）。

## 整頁視線圖的背景怎麼來

兩種背景，可在整頁視線圖切換：

- **完整長截圖（預設）**：每進入一個新網址，背景的無頭瀏覽器（看不到）會帶著錄製中的 cookies 開啟同一頁，一次截下整頁；頁面內捲動的網站會先把內容區展開。捲動再快也不會缺。動態內容可能與當時略有不同；只靠 localStorage 登入的網站可能截到登入頁，這時改看「錄製當下畫面」。
- **錄製當下畫面**：捲動停下、頁面載入完成且穩定時拍可視區，依捲動位置拼成；是你真正看到的畫面，但捲太快會有灰色缺口。

網址只差 `#…` 的視為同一頁。

## 攝影機模式的資料過濾

以下樣本會標為無效（`valid = 0`），統計在 `meta.json` 的 `source.rejected_samples`：

- 眨眼、偵測不到臉
- 頭部朝向／位置遠超出校正時的範圍（轉頭、低頭看鍵盤）
- 預測落在螢幕外超過半個螢幕寬（物理上不合理的追蹤失敗）

略超出螢幕的值會保留，例如看旁邊第二個螢幕時 `norm_x` ≈ 1.1–1.2。

## 限制

- `<canvas>`／WebGL、影片畫面不會出現在 rrweb 回放裡（座標仍然有記錄；整頁視線圖的截圖裡看得到）。
- 跨網域 iframe 會錄進回放；iframe 內的點擊也會用於點擊校正。回放時 iframe 內的滑鼠游標位置可能有偏移（rrweb 的限制），視線點不受影響。
- 注視點偵測用固定閾值（約 1° 視角 ≈ 50 實體像素、最短 100 ms），研究用途請依實驗設定調整 `export.detect_fixations`。
- 多螢幕且 DPI 不同時，請確認 `--monitor` 指向眼動儀設定的那個螢幕。

## 授權提醒

消費級 Tobii 眼動儀（如 Eye Tracker 5）的授權把「儲存、分析視線資料」歸為 *analytical* 用途，通常需要 Tobii 另外授權（或改用 Tobii Pro 系列 + Tobii Pro SDK）。本工具預設向 Stream Engine 宣告 `analytical`；如果裝置拒絕，錯誤訊息會說明。是否用 `--field-of-use interactive` 繞過，請先確認你的授權範圍。

rrweb（MIT 授權）內附於 `gazeweb/vendor/`。
