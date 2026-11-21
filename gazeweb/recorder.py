"""錄製：開瀏覽器 + 接視線來源，把每個視線樣本換算成網頁座標並和 rrweb 事件一起存檔。

座標換算鏈（每個樣本）：
  Tobii 正規化 (nx, ny)  ──顯示器矩形──▶  螢幕實體像素 (gx, gy)
  螢幕實體像素 ──瀏覽器內容區 HWND 矩形 / devicePixelRatio──▶  視窗座標 (vx, vy)，CSS px
  視窗座標 + 當下 scrollX/scrollY  ──▶  整頁座標 (page_x, page_y)
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional

from . import win32
from .sources import GazeSample, GazeSource
from .visits import page_key

HERE = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "gazeweb" / "browser-profile"
MAX_TILES = 400


def _looks_blank(jpeg: bytes, w_px: float, h_px: float) -> bool:
    """判斷截圖是否幾乎是單一顏色（頁面還沒畫出來）。有 Pillow 時看像素變異，否則看 JPEG 壓縮率。"""
    try:
        from PIL import Image, ImageStat
        im = Image.open(io.BytesIO(jpeg)).convert("L").resize((64, 36))
        return ImageStat.Stat(im).stddev[0] < 4.0
    except ImportError:
        # 單色畫面的 JPEG 極小；一般有內容的網頁每像素遠大於 0.012 bytes
        return len(jpeg) / max(1.0, w_px * h_px) < 0.012
    except Exception:  # noqa: BLE001
        return False


class Recorder:
    def __init__(self, source: GazeSource, monitor: win32.Monitor, out_dir: Path,
                 start_url: str, channel: Optional[str] = "chrome", fullpage_shots: bool = True,
                 duration_s: Optional[float] = None):
        self.duration_s = duration_s
        self.source = source
        self.monitor = monitor
        self.out_dir = out_dir
        self.start_url = start_url
        self.channel = channel
        self.fullpage_shots = fullpage_shots

        self.lock = threading.Lock()
        self.samples: list[dict] = []
        self.events: list[dict] = []
        self.tabs: dict[int, dict] = {}       # id(page) -> {"tab": n, "geom": {...}}
        self.active_tab: Optional[int] = None
        self.viewport_rect: Optional[tuple] = None
        self.browser_hwnd: Optional[int] = None
        self.url_visits: dict[str, dict] = {}  # url -> 最後一次的 iw/ih/dw/dh
        self.clicks: list[dict] = []
        self.visits: list[dict] = []            # 造訪段落：換頁或切分頁就開新段落
        self.tiles: dict[str, list[dict]] = {}  # page_key -> 錄製中拍的可視區截圖
        self._tile_data: dict[str, bytes] = {}  # 檔名 -> JPEG（被取代的舊圖會刪掉）
        self._tile_seq = 0
        self._last_tile = 0.0
        self._blank_tries: dict[tuple, int] = {}
        self._cookies: list = []
        self.full_shots: dict[str, dict] = {}   # page_key -> 背景完整長截圖
        self._fp_requested: set[str] = set()
        self._capturer = None
        self._stop = threading.Event()
        self.t_start = time.time() * 1000.0

    # ---------- 瀏覽器回報 ----------
    def _tab_of(self, page) -> dict:
        k = id(page)
        if k not in self.tabs:
            self.tabs[k] = {"tab": len(self.tabs) + 1, "geom": None, "page": page, "scroll_changed": 0.0,
                            "url_changed": 0.0, "last_mutation": 0.0, "cdp": None}
        return self.tabs[k]

    def on_geom(self, source, g: dict):
        with self.lock:
            tab = self._tab_of(source["page"])
            g["recv"] = time.time() * 1000.0
            prev = tab["geom"]
            if not prev or (prev["sx"], prev["sy"], prev["url"], prev["iw"], prev["ih"]) != (
                    g["sx"], g["sy"], g["url"], g["iw"], g["ih"]):
                tab["scroll_changed"] = g["recv"]
            if not prev or prev["url"] != g["url"]:
                tab["url_changed"] = g["recv"]
            tab["geom"] = g
            pid = id(source["page"])
            # 作用中分頁＝最後一個取得焦點的分頁。瀏覽器失去作業系統焦點（例如點了終端機）時不清除，
            # 因為最前面的分頁沒變；視線是否真的落在瀏覽器上，另外用螢幕上的視窗判斷。
            if g.get("focus") and g.get("vis") == "visible":
                self.active_tab = pid
            elif self.active_tab is None and g.get("reason") == "load":
                self.active_tab = pid  # 瀏覽器剛開、還沒被點過時，先以載入的頁面為準
            if self.active_tab == pid:
                key = page_key(g["url"])
                cur = self.visits[-1] if self.visits else None
                if cur and cur["tab"] == tab["tab"] and cur["key"] == key:
                    cur["end_ms"] = g["t"]
                    cur["title"] = g.get("title") or cur["title"]
                else:
                    if cur:
                        cur["end_ms"] = max(cur["end_ms"], g["t"])
                    self.visits.append({"tab": tab["tab"], "url": g["url"], "key": key,
                                        "title": g.get("title", ""), "start_ms": g["t"], "end_ms": g["t"]})
            self.url_visits[g["url"]] = {k: g[k] for k in ("iw", "ih", "dw", "dh", "dpr", "title")}
        if g.get("click") and hasattr(self.source, "add_reference"):
            self._click_reference(g)

    def _click_reference(self, g: dict):
        """把網頁上的點擊位置換成顯示器正規化座標，交給攝影機來源做線上校正。"""
        r = self.viewport_rect
        dpr = g["dpr"] or 1.0
        if not (r and abs(r[2] - g["iw"] * dpr) <= 4 and abs(r[3] - g["ih"] * dpr) <= 4):
            return
        c = g["click"]
        gx, gy = r[0] + c["cx"] * dpr, r[1] + c["cy"] * dpr
        m = self.monitor
        nx, ny = (gx - m.left) / m.width, (gy - m.top) / m.height
        threading.Thread(target=self.source.add_reference, args=(c["t"], nx, ny), daemon=True).start()
        with self.lock:
            self.clicks.append({"t_ms": c["t"], "screen_x": gx, "screen_y": gy, "url": g["url"]})

    def on_events(self, source, batch: list):
        with self.lock:
            tab = self._tab_of(source["page"])
            for e in batch:
                e["tab"] = tab["tab"]
                d = e.get("data") or {}
                # 記錄最後一次「明顯的」畫面變動（新文件，或一次增刪 3 個以上節點），截圖要等畫面穩定
                if e.get("type") == 2 or (e.get("type") == 3 and d.get("source") == 0
                                          and len(d.get("adds", [])) + len(d.get("removes", [])) >= 3):
                    tab["last_mutation"] = max(tab["last_mutation"], e.get("timestamp", 0))
            self.events.extend(batch)

    # ---------- 視窗矩形輪詢 ----------
    def _poll_viewport(self):
        """持續取得瀏覽器內容區的實體像素矩形。

        瀏覽器在前景時直接取前景視窗；不在前景時沿用記住的瀏覽器視窗（或用分頁標題找），
        讓「瀏覽器開著但焦點在別的程式」時仍能換算座標。
        """
        while not self._stop.is_set():
            try:
                with self.lock:
                    tab = self.tabs.get(self.active_tab) if self.active_tab else None
                    g = tab["geom"] if tab else None
                rect = win32.foreground_viewport_rect()
                if rect and g and self._size_matches(rect, g):
                    self.browser_hwnd = rect[4]
                elif g:
                    hwnd = self.browser_hwnd or win32.find_browser_window(g.get("title", ""))
                    rect = win32.viewport_rect_of(hwnd) if hwnd and win32.window_usable(hwnd) else None
                    if rect and self._size_matches(rect, g):
                        self.browser_hwnd = hwnd
                    else:
                        rect = None
                self.viewport_rect = rect
            except Exception:  # noqa: BLE001
                self.viewport_rect = None
            time.sleep(0.05)

    @staticmethod
    def _size_matches(rect, g) -> bool:
        dpr = g["dpr"] or 1.0
        return abs(rect[2] - g["iw"] * dpr) <= 4 and abs(rect[3] - g["ih"] * dpr) <= 4

    # ---------- 視線樣本 ----------
    def on_sample(self, s: GazeSample):
        m = self.monitor
        row = {
            "t_ms": round(s.t_ms, 3),
            "t_rel_ms": round(s.t_ms - self.t_start, 3),
            "device_ts_us": s.device_ts_us,
            "valid": int(s.valid),
            "norm_x": None, "norm_y": None, "screen_x": None, "screen_y": None,
            "viewport_x": None, "viewport_y": None, "page_x": None, "page_y": None,
            "scroll_x": None, "scroll_y": None, "in_viewport": 0,
            "url": None, "tab": None, "mapping": "none",
        }
        if s.valid and not math.isnan(s.nx):
            gx = m.left + s.nx * m.width
            gy = m.top + s.ny * m.height
            row.update(norm_x=round(s.nx, 6), norm_y=round(s.ny, 6), screen_x=round(gx, 1), screen_y=round(gy, 1))
            with self.lock:
                tab = self.tabs.get(self.active_tab) if self.active_tab else None
                g = tab["geom"] if tab else None
                tabno = tab["tab"] if tab else None
            if g:
                r = self.viewport_rect
                vx, vy, how = self._to_viewport(gx, gy, g)
                if how == "hwnd" and r and win32.root_at_point(gx, gy) != r[4]:
                    vx = None  # 視線落在其他視窗上（終端機、被蓋住的區域、別的螢幕），不算看網頁
                if vx is not None:
                    row.update(
                        viewport_x=round(vx, 1), viewport_y=round(vy, 1),
                        page_x=round(vx + g["sx"], 1), page_y=round(vy + g["sy"], 1),
                        scroll_x=round(g["sx"], 1), scroll_y=round(g["sy"], 1),
                        in_viewport=int(0 <= vx < g["iw"] and 0 <= vy < g["ih"]),
                        url=g["url"], tab=tabno, mapping=how,
                    )
        with self.lock:
            self.samples.append(row)

    def _to_viewport(self, gx: float, gy: float, g: dict):
        dpr = g["dpr"] or 1.0
        r = self.viewport_rect
        # 精確：前景瀏覽器內容區 HWND 的實體像素矩形，並確認大小和該分頁回報的一致
        if r and abs(r[2] - g["iw"] * dpr) <= 4 and abs(r[3] - g["ih"] * dpr) <= 4:
            return (gx - r[0]) / dpr, (gy - r[1]) / dpr, "hwnd"
        # 後備：用 JS 的 screenX/滑鼠事件估計內容區原點（假設頁面縮放 100%）
        mo = g.get("mouse")
        if mo:
            ox, oy = mo["sx"] - mo["cx"], mo["sy"] - mo["cy"]
        else:
            border = max(0, (g["ow"] - g["iw"]) / 2)
            ox, oy = g["scrX"] + border, g["scrY"] + (g["oh"] - g["ih"]) - border
        return gx / dpr - ox, gy / dpr - oy, "estimated"

    # ---------- 錄製中的可視區截圖 ----------
    # 截圖前畫面必須穩定的時間（毫秒）
    SETTLE_SCROLL_MS = 400     # 捲動停下
    SETTLE_URL_MS = 1200       # 換網址（含單頁應用的路由切換）後
    SETTLE_MUTATION_MS = 500   # 最後一次明顯的 DOM 增刪後
    BLANK_RETRIES = 3          # 同一位置拍到空白畫面時最多重試幾次

    def _capture_tile(self):
        """畫面穩定時拍一張目前可視區，之後依捲動位置拼成整頁圖（使用者當下真正看到的畫面）。

        條件：頁面載入完成、網址穩定 1.2 秒、捲動停下 0.4 秒、最近 0.6 秒沒有明顯的 DOM 變動。
        同一位置拍過之後，若頁面又有明顯變動（延遲載入、內容補齊），穩定後重拍並取代舊圖（最多每 5 秒一次）。
        拍到空白畫面不算數，之後再重拍。
        """
        now = time.time() * 1000.0
        if now - self._last_tile < 500 or len(self._tile_data) >= MAX_TILES:
            return
        with self.lock:
            tab = self.tabs.get(self.active_tab) if self.active_tab else None
            g = tab["geom"] if tab else None
            if not g:
                return
            last_mut = max(tab["last_mutation"], g.get("lm") or 0)
            # 不要求瀏覽器在前景：CDP 截圖不搶焦點；只要瀏覽器視窗可見且大小與此分頁相符
            if (self.viewport_rect is None or g.get("rs", "complete") != "complete"
                    or now - tab["scroll_changed"] < self.SETTLE_SCROLL_MS
                    or now - tab["url_changed"] < self.SETTLE_URL_MS
                    or now - last_mut < self.SETTLE_MUTATION_MS):
                return
            key = page_key(g["url"])
            if not key or not key.startswith(("http", "file")):
                return
            cur = self.visits[-1] if self.visits and self.visits[-1]["key"] == key else None
            visit_start = cur["start_ms"] if cur else 0
            # 這次造訪期間、同一位置已拍過的圖（之前造訪的圖留給那一段用，不取代）
            same = [t for t in self.tiles.get(key, [])
                    if abs(t["sy"] - g["sy"]) < 0.2 * g["ih"] and abs(t["sx"] - g["sx"]) < 0.2 * g["iw"]
                    and t["t"] >= visit_start]
            newest = max(same, key=lambda t: t["t"]) if same else None
            if newest and not newest.get("blank") and (newest["t"] > last_mut or now - newest["t"] < 5000):
                return  # 拍完之後畫面沒再明顯變動，或剛拍過
            band = (key, visit_start, round(g["sy"] / max(1, 0.2 * g["ih"])), round(g["sx"] / max(1, 0.2 * g["iw"])))
            page, cdp = tab["page"], tab["cdp"]
        self._last_tile = now
        data = self._screenshot(page, cdp)
        if not data:
            return
        blank = _looks_blank(data, g["iw"] * (g["dpr"] or 1), g["ih"] * (g["dpr"] or 1))
        with self.lock:
            if blank:
                self._blank_tries[band] = self._blank_tries.get(band, 0) + 1
                if self._blank_tries[band] < self.BLANK_RETRIES or (newest and not newest.get("blank")):
                    return  # 多半是還沒畫出來，稍後重拍；已有正常圖時不要用空白圖取代
            lst = self.tiles.setdefault(key, [])
            for old in same:  # 取代這次造訪同位置的舊圖
                if old in lst:
                    lst.remove(old)
                    self._tile_data.pop(old["file"], None)
            self._tile_seq += 1
            name = f"pages/tiles/tile_{self._tile_seq:04d}.jpg"
            self._tile_data[name] = data
            lst.append({"file": name, "sx": g["sx"], "sy": g["sy"], "w": g["iw"], "h": g["ih"],
                        "dpr": g["dpr"], "t": now, "dw": g.get("dw"), "dh": g.get("dh"), "blank": blank})

    @staticmethod
    def _screenshot(page, cdp) -> Optional[bytes]:
        """直接用 CDP 從已畫好的畫面擷取，不改動頁面。

        Playwright 的 page.screenshot 會暫時調整頁面（縮放、隱藏游標、等待字型），
        在有視窗的瀏覽器上會造成白色閃爍。
        """
        if cdp is not None:
            try:
                r = cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 70, "fromSurface": True})
                return base64.b64decode(r["data"])
            except Exception:  # noqa: BLE001
                return None
        try:
            return page.screenshot(type="jpeg", quality=70, scale="device", caret="initial",
                                   animations="allow", timeout=3000)
        except Exception:  # noqa: BLE001
            return None

    # ---------- 主流程 ----------
    def tick(self, page):
        """主迴圈每 200 ms 呼叫一次（在 Playwright 執行緒）；子類別可覆寫做自動化測試。"""

    def run(self):
        from playwright.sync_api import Error as PwError, sync_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        rrweb_js = (HERE / "vendor" / "rrweb.min.js").read_text(encoding="utf-8")
        inject_js = (HERE / "inject.js").read_text(encoding="utf-8")
        closed = threading.Event()

        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel=self.channel or None, headless=False,
                no_viewport=True, bypass_csp=True, args=["--start-maximized"],
                # Playwright 預設強制淺色模式；系統是深色時，YouTube 等網站會切換主題、
                # 甚至帶 themeRefresh=1 重新載入，造成白色閃爍。改成跟隨系統設定。
                color_scheme="null",
            )
            ctx.on("close", lambda *_: closed.set())
            ctx.expose_binding("__gazewebGeom", self.on_geom)
            ctx.expose_binding("__gazewebEvents", self.on_events)
            ctx.add_init_script(script=rrweb_js + "\n;\n" + inject_js)

            cdp_sessions = []

            def real_focus(p):
                # Playwright 預設讓每個分頁都以為自己有焦點（focus emulation），
                # 背景分頁也會回報「我是作用中分頁」，視線會被歸到錯的分頁、段落來回跳。關掉它。
                # 必須保留 CDP session 的參照，且每次主框架導覽後重送（跨行程導覽會重設這個設定）。
                try:
                    s = ctx.new_cdp_session(p)
                    cdp_sessions.append(s)
                except Exception:  # noqa: BLE001
                    return
                with self.lock:
                    self._tab_of(p)["cdp"] = s  # 也用來截圖（不改動頁面，不會閃爍）

                def apply(*_):
                    try:
                        s.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
                    except Exception:  # noqa: BLE001
                        pass

                apply()
                p.on("framenavigated", lambda f: apply() if f == p.main_frame else None)
                p.on("domcontentloaded", apply)

            def track_close(p):
                def closed_page(*_):
                    with self.lock:
                        if self.active_tab == id(p):
                            self.active_tab = None
                p.on("close", closed_page)

            for fn in (real_focus, track_close):
                ctx.on("page", fn)
                for p in ctx.pages:
                    fn(p)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            threading.Thread(target=self._poll_viewport, daemon=True).start()
            if self.fullpage_shots:
                from .fullpage import FullPageCapturer
                self._capturer = FullPageCapturer(self.channel, self._on_fullpage)
                self._capturer.start()
            self.t_start = time.time() * 1000.0
            self.source.start(self.on_sample)
            print("● 錄製中。在瀏覽器裡正常瀏覽；結束請關閉瀏覽器視窗，或在此按 Ctrl+C。")
            try:
                try:
                    page.goto(self.start_url)
                except PwError as e:
                    print(f"[warn] 開啟起始網址失敗：{e}")
                while not closed.is_set():
                    pages = [p for p in ctx.pages if not p.is_closed()]
                    if not pages:
                        break
                    if self.duration_s and time.time() * 1000.0 - self.t_start > self.duration_s * 1000:
                        print("已達指定錄製時間。")
                        break
                    try:
                        self.tick(pages[0])
                        self._capture_tile()
                        self._request_fullpage(ctx)
                        pages[0].wait_for_timeout(200)  # 讓 Playwright 派送 binding 回呼
                    except PwError:
                        time.sleep(0.2)
            except KeyboardInterrupt:
                print("\n收到 Ctrl+C，停止錄製…")
            finally:
                self.source.stop()
                self._stop.set()
                self.t_end = time.time() * 1000.0
                for p in list(ctx.pages):  # 把頁面上還沒送出的事件收回來
                    try:
                        p.evaluate("dispatchEvent(new Event('pagehide'))")
                        p.wait_for_timeout(100)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

        self.save()  # 先存資料，長截圖還沒完成也不會遺失錄製
        if self._capturer:
            # 錄製中沒來得及排程的網址（停留太短）補排，再等背景長截圖做完
            for url in list(self.url_visits):
                key = page_key(url)
                if key and key.startswith("http") and key not in self._fp_requested:
                    v = self.url_visits[url]
                    self._fp_requested.add(key)
                    self._capturer.submit({"key": key, "url": url, "cookies": self._cookies, "iw": v["iw"], "ih": v["ih"]})
            pending = self._capturer.jobs.qsize() + (1 if self._capturer.busy.is_set() else 0)
            if pending:
                print(f"等待背景長截圖完成（剩 {pending} 頁）…")
            self._capturer.finish()
            for msg in self._capturer.failed:
                print(f"[warn] 長截圖失敗 {msg}")
            if self.full_shots:
                self.save()
        return self.out_dir

    # ---------- 背景完整長截圖 ----------
    SETTLE_FULLPAGE_MS = 1500

    def _request_fullpage(self, ctx):
        """作用中頁面載入完成、網址穩定後，排程一張背景長截圖（每個網址一次）。"""
        if not self._capturer:
            return
        now = time.time() * 1000.0
        with self.lock:
            tab = self.tabs.get(self.active_tab) if self.active_tab else None
            g = tab["geom"] if tab else None
            if (not g or g.get("rs", "complete") != "complete"
                    or now - tab["url_changed"] < self.SETTLE_FULLPAGE_MS):
                return
            key = page_key(g["url"])
            if not key or not key.startswith("http") or key in self._fp_requested:
                return
            self._fp_requested.add(key)
        try:
            # 只取 cookies：ctx.storage_state() 會替「造訪過但目前沒開著」的網域各開一個分頁，造成跳出新分頁
            self._cookies = ctx.cookies()
        except Exception:  # noqa: BLE001
            pass
        self._capturer.submit({"key": key, "url": g["url"], "cookies": self._cookies,
                               "iw": g["iw"], "ih": g["ih"], "t": now})

    def _on_fullpage(self, job: dict, data: bytes, width: int, height: int):
        with self.lock:
            name = f"pages/full_{len(self.full_shots) + 1:02d}.jpg"
            self._tile_data[name] = data
            self.full_shots[job["key"]] = {"file": name, "width": width, "height": height,
                                           "url": job["url"], "t": time.time() * 1000.0}

    def save(self) -> Path:
        from .export import write_session

        with self.lock:
            events = sorted(self.events, key=lambda e: e["timestamp"])
            samples = list(self.samples)
            tile_data = dict(self._tile_data)
            tiles = {k: list(v) for k, v in self.tiles.items()}
            shots = {k: dict(v) for k, v in self.full_shots.items()}
        if not samples and not events:
            print("沒有錄到任何資料，不建立資料夾。")
            return self.out_dir
        for name, b in tile_data.items():
            p = self.out_dir / name
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b)
        meta = {
            "format": "gazeweb-session/1",
            "scroll_model": "main-scroller",  # 捲動量已包含頁面內主要捲動容器（見 inject.js）
            "started_ms": self.t_start,
            "ended_ms": getattr(self, "t_end", time.time() * 1000.0),
            "source": self.source.describe(),
            "monitor": self.monitor.as_dict(),
            "browser_channel": self.channel or "chromium",
            "start_url": self.start_url,
            "urls": self.url_visits,
            "page_shots": shots,   # 背景完整長截圖（每個網址一張）
            "page_tiles": tiles,   # 錄製當下的可視區截圖
            "visits": [dict(v) for v in self.visits],
            "clicks": self.clicks,
        }
        write_session(self.out_dir, meta, samples, events)
        return self.out_dir
