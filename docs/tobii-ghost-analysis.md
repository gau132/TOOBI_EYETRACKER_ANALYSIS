# Tobii Ghost 1.14.1 眼動追蹤運作分析（逆向證據）

分析對象：`%LOCALAPPDATA%\TobiiGhost\app-1.14.1\`（由 `TobiiGhost.1.14.1-Setup.exe` 安裝，Squirrel 安裝器）。
方法：用 REA 對 .NET 組件做靜態 metadata／CIL 分析（不執行目標程式碼），再對照本機設定檔、日誌，並用 ctypes 實際呼叫附帶的 Stream Engine。

> 標記：**[觀察]** = 直接從檔案/metadata 讀到；**[推論]** = 由觀察合理推得；**[未知]** = 本次沒有證據。

## 一句話結論

Ghost **不自己做影像處理**。眼睛偵測、瞳孔/角膜反光、3D 視線計算都在眼動儀韌體與 Tobii 服務（Tobii Experience）裡完成。Ghost 只透過 **Tobii Stream Engine**（`tobii_stream_engine.dll`）訂閱一個串流：**gaze point**，拿到「螢幕上的正規化 2D 座標 + 時間戳 + 有效旗標」，再畫成泡泡疊加到畫面或推到 Twitch。

## 架構與資料流

```
眼動儀 (紅外光源 + 相機，PCCR)
   │  韌體 / Tobii Experience 服務：偵測瞳孔與角膜反光 → 3D 視線 → 與螢幕平面相交
   ▼
tobii_stream_engine.dll 4.8.0.1  (x64/ x86/)
   │  tobii_gaze_point_subscribe → callback(tobii_gaze_point_t*)
   ▼
Tobii.Gaming.Tracking.Net.dll 1.3.0.0  (P/Invoke 封裝)
   │  StreamEngineDevice.SubscribeToGazePoints
   ▼
TobiiGhost.dll  Tobii.Ghost.Tracking.TrackerController.GazePointCallback
   │  檢查 validity → 取 position.x / position.y → timestamp_us 轉 UTC
   ▼
GazePointProducer.Update → PointRecording.Record (ConcurrentQueue<TimestampedPoint>)
   │
   ├─ PointRenderer.RenderPoints → MemoryFileWriter("GazePoints") 共享記憶體
   │        └─ SSOverlay.exe / PreviewOverlay.exe (+ GazeRenderer.dll) 讀取並畫泡泡
   └─ TwitchPackageBuilder → Twitch 擴充功能
```

## 證據

### 1. 用的是 Tobii Stream Engine **[觀察]**
- `x64\tobii_stream_engine.dll` FileVersion `4.8.0.1`，描述 "Tobii Stream Engine API library"；另有 `middleware_client.dll`（"Tobii Middleware Client API library"）。
- `Tobii.Gaming.Tracking.Net.dll`（SHA-256 `83c885ef…8a7a`）的 ModuleRef 為：`tobii_stream_engine`、`middleware_client`、`user32.dll`、`CfgMgr32.dll`、`kernel32`、`Shcore.dll`；共 110 個 P/Invoke 匯入。
  REA Evidence：`ev_d6a47fda90923d896c5aa612e6af2b09edb320938ac3634c6aad69bac8e68e8c`

### 2. Ghost 實際只訂閱 gaze point（和 user presence 的包裝） **[觀察]**
雖然 Interop 宣告了 gaze_origin、head_pose、eye_position_normalized、校正等函式，但 `StreamEngineDevice` 的呼叫邊只用到：

| 方法 | 呼叫的原生函式 |
|---|---|
| `StreamEngineApi..ctor` | `tobii_api_create` |
| `StreamEngineApi.GetTrackerDeviceUrls` | `tobii_enumerate_local_device_urls_ex` |
| `StreamEngineApi.GetSystemClock` | `tobii_system_clock` |
| `StreamEngineDevice..ctor` | `tobii_device_create_ex`（有授權金鑰時）或 `tobii_device_create` |
| `StreamEngineDevice.SubscribeToGazePoints` | `tobii_gaze_point_subscribe` |
| `StreamEngineDevice.TryWaitForCallbacks` | `tobii_wait_for_callbacks` |
| `StreamEngineDevice.TryProcessCallbacks` | `tobii_device_process_callbacks` |
| `StreamEngineDevice.TryReconnect` | `tobii_device_reconnect` |
| `StreamEngineDevice.GetDisplayId` | `tobii_get_display_id` |

`TobiiGhost.dll`（SHA-256 `462a8c18…eae`）中 `TrackerController.SetDevice` 建立 `tobii_gaze_point_callback_t` 並呼叫 `SubscribeToGazePoints`；整個 Ghost 沒有呼叫 gaze_origin / head_pose 等訂閱。
REA Evidence：`ev_9182a0dfe7c96918f2faccf7f31e5192c267fc60abebf28983cc52d414e867f3`、`ev_905f75dceab6ad1d2496aaa833884c1bd781601faf7e18518ed968704e7f0f54`

### 3. gaze point 的資料格式 **[觀察]**
`Tobii.StreamEngine.tobii_gaze_point_t` 欄位：`timestamp_us`（int64）、`validity`（`tobii_validity_t`：`TOBII_VALIDITY_INVALID` / `TOBII_VALIDITY_VALID`）、`position`（`TobiiVector2`，x/y float）。
`TrackerController.GazePointCallback` 的欄位存取依序是：`validity` → `position.x`、`position.y` → `_getUtcTimestamp(timestamp_us)` → 呼叫 `_onGazePoint`（`Action<x, y, time>`）。
**[推論]** x、y 是相對於設定顯示器的 0–1 正規化座標（Stream Engine 公開文件的定義；Ghost 之後用 `MonitorInfo.MonitorArea` 乘回像素，並把 `TrackerScreenBounds` 寫入共享記憶體給疊加層）。

### 4. 時間對齊 **[觀察]**
`TrackerController.Create` 呼叫順序：`DateTime.UtcNow` → `IStreamEngineApi.GetSystemClock`（`tobii_system_clock`）→ `DateTime.UtcNow` → 相減取中點 → 建立 `TimestampSyncPoint(timestamp_us, utc)`；`TimestampSyncPoint.GetUtcTimeStamp` 用 `DateTime.AddTicks` 把裝置微秒換成 UTC。

### 5. 呼叫模型：輪詢執行緒 **[觀察]**
`GazePointProducer.Update` → `ITrackerController.UpdateTracking` → `TryWaitForCallbacks` + `TryProcessCallbacks`；`#US` 字串中有執行緒名稱 `PointProducerThread`、`GazeRenderThread`、`TrackerSelectionThread`、`ObserveTrackerMonitorThread`。連線失敗時 `OnConnectionFailed` → `TryReconnect` / `TryClearCallbackBuffers`。

### 6. 輸出到疊加層 **[觀察]**
- `EntryPoint.Main` 建立三個 `MemoryFileWriter`；字串常數 `GazePoints`、`GazeSettings`、`TrackerScreenBounds`。
- `PointRenderer.RenderPoints` 寫入 `MemoryFileWriter`；`OverlayController.WriteSettingsToSharedMemory` 寫入泡泡顏色、形狀、大小、Trail、Decay、Responsiveness（對應 `%LOCALAPPDATA%\TobiiGhostData\Settings\visualizationSettings`）。
- 執行中的程序：`TobiiGhost.exe`、`SSOverlay.exe`、`PreviewOverlay.exe preview`、CefSharp 子程序（Twitch 登入等網頁 UI）。
- **[推論]** 平滑（Responsiveness）與淡出（Decay/Trail）在疊加層 / `GazeRenderer.dll` 端進行；**[未知]** 確切濾波演算法（`GazeRenderer.dll` 為原生 DLL，本次未反組譯）。

### 7. 螢幕對應 **[觀察]**
`Tobii.Gaming.Tracking.Net.dll` P/Invoke：`EnumDisplayMonitors`、`GetMonitorInfo`、`EnumDisplayDevices`、`CM_Get_Device_Interface_Property`、`SetProcessDpiAwareness`、`GetDpiForMonitor`；`TrackerMonitorObserver` 以 `tobii_get_display_id`（或 middleware 的 `tobii_middleware_get_display_info`）找出眼動儀綁定的顯示器。

### 8. 用途宣告 **[觀察]**
`tobii_device_create` 的第三個參數是 `tobii_field_of_use_t`，Interop 中定義 `TOBII_FIELD_OF_USE_INTERACTIVE` 與 `TOBII_FIELD_OF_USE_ANALYTICAL`。消費級眼動儀的授權把「儲存／分析視線資料」歸為 analytical 用途。

### 9. 本機現況 **[觀察]**
- 用附帶的 Stream Engine 實測：`tobii_get_api_version` = 4.8.0.1，`tobii_api_create` 成功，`tobii_enumerate_local_device_urls` 回傳 **0 台裝置**。
- 系統服務中找不到 Tobii 服務；`%LOCALAPPDATA%\Tobii\Installer` 有 2026-07-20 的 Tobii Experience（EyeTracker5）安裝日誌。
- **[推論]** 眼動儀目前沒接上，或 Tobii Experience 執行環境沒有在執行。要讓本工具取得真實資料，要先讓 Tobii Experience 偵測到裝置。

## 這對我們工具的意義
1. 不需要（也不應該）碰 Ghost 本身：直接用同一顆 `tobii_stream_engine.dll` 訂閱 gaze point 就能拿到相同的資料。
2. 資料是**螢幕座標**，要對應到網頁，還需要知道瀏覽器內容區在螢幕上的位置、DPI 縮放、捲動量 → 本工具用 Win32 視窗矩形 + `devicePixelRatio` + 頁面回報的 scroll 解決。
3. 要「回放網站」，必須另外錄下 DOM（rrweb），因為 Tobii 只給座標、不知道畫面內容。
