"""命令列：python -m gazeweb <devices|record|rebuild>"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import win32


def cmd_devices(a):
    print("顯示器：")
    for mon in win32.monitors():
        print(f"  [{mon.index}] {mon.name}  {mon.width}x{mon.height} @({mon.left},{mon.top})"
              + ("  主要" if mon.primary else ""))
    try:
        from .tobii import StreamEngine
        se = StreamEngine(a.dll)
        urls = se.list_devices()
        print(f"Stream Engine：{se.dll_path}")
        print("眼動儀：" + (", ".join(urls) if urls else "（找不到。確認裝置已接上且 Tobii Experience 正在執行）"))
        se.close()
    except Exception as e:  # noqa: BLE001
        print(f"Stream Engine：無法使用 — {e}")


def _chrome_installed() -> bool:
    import os
    roots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    return any(Path(r, "Google", "Chrome", "Application", "chrome.exe").is_file() for r in roots if r)


def cmd_record(a):
    from .recorder import Recorder

    monitor = win32.pick_monitor(a.monitor)
    if a.source == "tobii":
        from .tobii import TobiiGazeSource
        try:
            source = TobiiGazeSource(a.dll, a.device, a.field_of_use)
        except Exception as e:  # noqa: BLE001
            print(f"無法連線眼動儀：{e}\n可先用 --source webcam（攝影機）或 --source mouse（滑鼠模擬）。")
            sys.exit(1)
    elif a.source == "webcam":
        source = _webcam_source(a, monitor)
    else:
        from .sources import MouseGazeSource
        source = MouseGazeSource(monitor)
    if a.browser == "auto":
        a.browser = "chrome" if _chrome_installed() else "msedge"
    out = Path(a.out) / time.strftime("%Y%m%d-%H%M%S")
    print(f"視線來源：{source.describe()}；顯示器 [{monitor.index}] {monitor.width}x{monitor.height}")
    rec = Recorder(source, monitor, out, a.url, channel=None if a.browser == "chromium" else a.browser,
                   fullpage_shots=not a.no_screenshots, duration_s=a.duration)
    path = rec.run()
    print(f"開啟回放：{path / 'replay.html'}")


def _webcam_calibrate(a, monitor, force_full: bool = False):
    """開攝影機，做錄製前定位或完整校正，回傳 (tracker, calibration, 定位殘差)。"""
    from .calibrate import CalibrationUI
    from .webcam import CALIB_PATH, Calibration, FaceTracker

    tracker = FaceTracker(a.camera)
    try:
        tracker.start()
    except RuntimeError as e:
        print(f"攝影機無法使用：{e}")
        sys.exit(1)
    ui = CalibrationUI(tracker, monitor, points=a.points, distance_cm=a.distance)
    age_min = (time.time() - CALIB_PATH.stat().st_mtime) / 60 if CALIB_PATH.is_file() else None
    if not force_full and not getattr(a, "recalibrate", False) and age_min is not None and age_min <= QUICK_CHECK_MAX_AGE_MIN:
        # 錄製前定位：沿用最近的校正，5 點量出目前偏差當初始偏移；錄製中點擊再並行修正
        calib = Calibration.load()
        res = ui.quick_check(calib)
        if res is None:
            tracker.stop()
            print("已取消。")
            sys.exit(1)
        status, seeds, rep = res
        if status == "ok":
            calib.report = {**calib.report, **rep}
            print(f"定位完成（沿用 {age_min:.0f} 分鐘前的校正）：偏差 {rep['positioning_before_px']:.0f} px → "
                  f"修正後約 {rep['positioning_after_px']:.0f} px")
            return tracker, calib, seeds
        print("改做完整校正。")
    calib = ui.run()
    if calib is None:
        tracker.stop()
        print("已取消校正。")
        sys.exit(1)
    calib.save(camera=a.camera, monitor=monitor.as_dict())
    r = calib.report
    extra = f"，約 {r['mean_error_cm']} cm / {r['mean_error_deg']}°" if "mean_error_deg" in r else ""
    print(f"校正完成：驗證平均誤差 {r['mean_error_px']} px{extra}")
    return tracker, calib, []


QUICK_CHECK_MAX_AGE_MIN = 30  # 這段時間內的校正：錄製前只做 5 點定位；更舊的做完整校正


def _webcam_source(a, monitor):
    from .webcam import WebcamGazeSource
    tracker, calib, seeds = _webcam_calibrate(a, monitor)
    src = WebcamGazeSource(tracker, calib, click_calibration=not a.no_click_calibration)
    if seeds:
        src.seed_offset(seeds)  # 開始時的定位與之後的點擊，一起估計偏移
    return src


def cmd_calibrate(a):
    from .calibrate import live_test
    monitor = win32.pick_monitor(a.monitor)
    tracker, calib, _ = _webcam_calibrate(a, monitor, force_full=True)
    try:
        live_test(tracker, calib, monitor)
    finally:
        tracker.stop()


def _add_webcam_args(s):
    g = s.add_argument_group("攝影機（--source webcam）")
    g.add_argument("--camera", type=int, default=0, help="攝影機編號（預設 0）")
    g.add_argument("--points", type=int, choices=[9, 13], default=13, help="校正點數（預設 13）")
    g.add_argument("--distance", type=float, default=60.0, help="眼睛到螢幕的距離 cm，用來換算視角誤差（預設 60）")
    g.add_argument("--recalibrate", action="store_true",
                   help="錄製前一律做完整校正（預設：30 分鐘內校正過就只做 5 點定位）")
    g.add_argument("--reuse-calibration", action="store_true", help=argparse.SUPPRESS)  # 舊參數，已改為預設行為


def cmd_rebuild(a):
    from .export import rebuild
    rebuild(Path(a.session))


def main(argv=None):
    p = argparse.ArgumentParser(prog="gazeweb", description="Tobii 眼動儀 × 瀏覽器：錄製、回放、匯出視線座標")
    p.add_argument("--dll", help="tobii_stream_engine.dll 路徑（預設自動尋找 Tobii Ghost 附帶的版本）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("devices", help="列出顯示器與眼動儀")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("record", help="開啟瀏覽器開始錄製")
    s.add_argument("url", nargs="?", default="https://www.google.com", help="起始網址")
    s.add_argument("--source", choices=["tobii", "webcam", "mouse"], default="tobii",
                   help="視線來源：tobii 眼動儀／webcam 攝影機（先校正）／mouse 用滑鼠模擬")
    s.add_argument("--device", help="指定眼動儀 URL（預設第一台）")
    s.add_argument("--monitor", type=int, help="眼動儀所在的顯示器編號（預設主要顯示器；見 devices）")
    s.add_argument("--browser", choices=["auto", "chrome", "msedge", "chromium"], default="auto",
                   help="auto＝有 Chrome 用 Chrome、否則用 Edge；chromium＝Playwright 下載的 Chromium")
    s.add_argument("--field-of-use", choices=["analytical", "interactive"], default="analytical",
                   help="向 Stream Engine 宣告的用途；儲存視線資料屬於 analytical")
    s.add_argument("--out", default="sessions", help="輸出資料夾（預設 ./sessions）")
    s.add_argument("--no-screenshots", action="store_true", help="不拍背景完整長截圖（仍會拍錄製當下的可視區畫面）")
    s.add_argument("--duration", type=float, help="錄製秒數上限（預設不限，關閉瀏覽器或 Ctrl+C 結束）")
    _add_webcam_args(s)
    s.add_argument("--no-click-calibration", action="store_true",
                   help="攝影機模式下，不要用網頁上的點擊位置做線上校正")
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("calibrate", help="只做攝影機校正，接著全螢幕即時測試準確度")
    s.add_argument("--monitor", type=int, help="要校正的顯示器編號（預設主要顯示器）")
    _add_webcam_args(s)
    s.set_defaults(fn=cmd_calibrate)

    s = sub.add_parser("rebuild", help="從既有錄製重新產生 CSV / 注視點 / replay.html")
    s.add_argument("session", help="錄製資料夾，例如 sessions/20261018-120000")
    s.set_defaults(fn=cmd_rebuild)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
