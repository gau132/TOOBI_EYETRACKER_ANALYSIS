"""背景完整長截圖：在另一條執行緒用無頭瀏覽器（使用者看不到）帶著錄製中的 cookies 開啟同一頁，
一次截下整頁。錄製中的可視區截圖會因捲動太快而有缺口，長截圖補上完整的頁面。

頁面若是「頁面內捲動」（HackMD、Notion…），先把主要捲動容器展開成內容的完整高度再截，
這樣長截圖的 y 座標 = 容器上緣 + 容器內捲動位置，與錄製時的整頁座標（視窗座標 + 捲動量）一致。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

MAX_HEIGHT = 16000  # Chromium 截圖高度上限附近，避免極長頁面耗盡記憶體

EXPAND_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight;
  let best = null, bestH = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (el.scrollHeight <= el.clientHeight + 10) continue;
    const cs = getComputedStyle(el);
    if (!/(auto|scroll|overlay)/.test(cs.overflowY)) continue;
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (w * h < 0.5 * vw * vh) continue;
    if (el.scrollHeight > bestH) { best = el; bestH = el.scrollHeight; }
  }
  const doc = document.scrollingElement || document.documentElement;
  if (!best) return { inner: false, height: doc.scrollHeight };
  const top = best.getBoundingClientRect().top + scrollY;
  // 展開容器與其祖先，讓內容自然撐開整份文件
  for (let el = best; el && el !== document.documentElement; el = el.parentElement) {
    el.style.setProperty('overflow', 'visible', 'important');
    el.style.setProperty('max-height', 'none', 'important');
    if (el === best) el.style.setProperty('height', best.scrollHeight + 'px', 'important');
    else el.style.setProperty('height', 'auto', 'important');
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.position === 'absolute') {
      if (el === best) { el.style.setProperty('position', 'absolute', 'important'); el.style.setProperty('bottom', 'auto', 'important'); }
    }
  }
  document.documentElement.style.setProperty('overflow', 'visible', 'important');
  document.documentElement.style.setProperty('height', 'auto', 'important');
  document.body.style.setProperty('overflow', 'visible', 'important');
  document.body.style.setProperty('height', 'auto', 'important');
  return { inner: true, top, height: Math.max(doc.scrollHeight, top + bestH) };
}
"""

# 在無頭頁面裡逐段捲到底再回頂端，觸發延遲載入的圖片（不影響使用者）
LAZY_JS = r"""
async (maxH) => {
  const step = Math.max(400, innerHeight * 0.8);
  const end = Math.min(maxH, (document.scrollingElement || document.documentElement).scrollHeight);
  for (let y = 0; y < end; y += step) { scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }
  scrollTo(0, 0); await new Promise(r => setTimeout(r, 300));
}
"""


class FullPageCapturer(threading.Thread):
    """排隊處理長截圖工作；每個工作完成時呼叫 on_done(job, jpeg_bytes, width, height)。"""

    def __init__(self, channel: Optional[str], on_done: Callable[[dict, bytes, int, int], None]):
        super().__init__(name="FullPageCapturer", daemon=True)
        self.channel = channel
        self.on_done = on_done
        self.jobs: "queue.Queue[Optional[dict]]" = queue.Queue()
        self.busy = threading.Event()
        self.failed: list[str] = []

    def submit(self, job: dict):
        self.jobs.put(job)

    def finish(self, timeout_s: float = 90.0):
        """錄製結束：做完排隊中的工作（最多等 timeout_s 秒）。"""
        self.jobs.put(None)
        self.join(timeout_s)

    def run(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel=self.channel or None, headless=True)
            except Exception as e:  # noqa: BLE001
                self.failed.append(f"無法啟動背景瀏覽器：{e}")
                return
            try:
                while True:
                    job = self.jobs.get()
                    if job is None:
                        break
                    self.busy.set()
                    try:
                        self._capture(browser, job)
                    except Exception as e:  # noqa: BLE001
                        self.failed.append(f"{job.get('url')}: {str(e).splitlines()[0]}")
                    finally:
                        self.busy.clear()
            finally:
                browser.close()

    def _capture(self, browser, job: dict):
        ctx = browser.new_context(viewport={"width": int(job["iw"]), "height": int(job["ih"])},
                                  device_scale_factor=1, color_scheme="null")
        try:
            if job.get("cookies"):
                try:
                    ctx.add_cookies(job["cookies"])
                except Exception:  # noqa: BLE001
                    pass
            page = ctx.new_page()
            page.goto(job["url"], wait_until="load", timeout=30000)
            page.wait_for_timeout(1500)
            page.evaluate(LAZY_JS, MAX_HEIGHT)
            info = page.evaluate(EXPAND_JS)
            page.wait_for_timeout(800 if info.get("inner") else 300)
            if (info.get("height") or 0) > MAX_HEIGHT:
                # 極長頁面只截前 MAX_HEIGHT 像素
                data = page.screenshot(type="jpeg", quality=70, full_page=True, timeout=30000,
                                       clip={"x": 0, "y": 0, "width": int(job["iw"]), "height": MAX_HEIGHT})
            else:
                data = page.screenshot(type="jpeg", quality=70, full_page=True, timeout=30000)
            size = page.evaluate("[Math.max(innerWidth, (document.scrollingElement||document.documentElement).scrollWidth),"
                                 " (document.scrollingElement||document.documentElement).scrollHeight]")
            # device_scale_factor=1：圖片像素 = CSS 像素 = 錄製時的整頁座標
            self.on_done(job, data, int(size[0]), int(min(MAX_HEIGHT, size[1])))
        finally:
            ctx.close()
