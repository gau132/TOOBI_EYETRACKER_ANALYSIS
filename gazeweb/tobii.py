"""Tobii Stream Engine 的 ctypes 封裝。

呼叫流程與 Tobii Ghost 1.14.1 (Tobii.Gaming.Tracking.Net.dll) 逆向出的路徑一致：
tobii_api_create -> tobii_enumerate_local_device_urls -> tobii_device_create
-> tobii_gaze_point_subscribe -> 迴圈 tobii_wait_for_callbacks + tobii_device_process_callbacks
連線中斷時 tobii_device_reconnect；時間戳以 tobii_system_clock 與系統 UTC 對時。
"""
from __future__ import annotations

import ctypes as C
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .sources import GazeSample, GazeSource

# tobii_error_t（Stream Engine 4.x；Ghost 的 Interop 中此 enum 有 21 個常數，與下表相符）
ERROR_NAMES = {
    0: "NO_ERROR", 1: "INTERNAL", 2: "INSUFFICIENT_LICENSE", 3: "NOT_SUPPORTED",
    4: "NOT_AVAILABLE", 5: "CONNECTION_FAILED", 6: "TIMED_OUT", 7: "ALLOCATION_FAILED",
    8: "INVALID_PARAMETER", 9: "CALIBRATION_ALREADY_STARTED", 10: "CALIBRATION_NOT_STARTED",
    11: "ALREADY_SUBSCRIBED", 12: "NOT_SUBSCRIBED", 13: "OPERATION_FAILED",
    14: "CONFLICTING_API_INSTANCES", 15: "CALIBRATION_BUSY", 16: "CALLBACK_IN_PROGRESS",
    17: "TOO_MANY_SUBSCRIBERS", 18: "CONNECTION_FAILED_DRIVER", 19: "UNAUTHORIZED",
    20: "FIRMWARE_UPGRADE_IN_PROGRESS",
}
ERR_TIMED_OUT = 6  # wait_for_callbacks 逾時，不是錯誤
ERR_CONNECTION_FAILED = 5
ERR_CONNECTION_FAILED_DRIVER = 18

# tobii_field_of_use_t（Ghost 的 Interop 中也定義了這兩個值）
FIELD_OF_USE = {"interactive": 1, "analytical": 2}

TOBII_VALIDITY_VALID = 1


class tobii_gaze_point_t(C.Structure):
    _fields_ = [
        ("timestamp_us", C.c_int64),
        ("validity", C.c_int),
        ("position_xy", C.c_float * 2),
    ]


GAZE_CB = C.CFUNCTYPE(None, C.POINTER(tobii_gaze_point_t), C.c_void_p)
URL_CB = C.CFUNCTYPE(None, C.c_char_p, C.c_void_p)


def default_dll_candidates() -> list[Path]:
    """依序尋找可用的 tobii_stream_engine.dll（64-bit）。"""
    out: list[Path] = []
    env = os.environ.get("TOBII_STREAM_ENGINE_DLL")
    if env:
        out.append(Path(env))
    here = Path(__file__).resolve().parent
    out.append(here / "vendor" / "tobii_stream_engine.dll")
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    # Tobii Ghost 隨附的 Stream Engine
    out.extend(sorted(local.glob("TobiiGhost/app-*/x64/tobii_stream_engine.dll"), reverse=True))
    out.extend(sorted(local.glob("TobiiGameHub/app-*/x64/tobii_stream_engine.dll"), reverse=True))
    return out


def _err(code: int) -> str:
    return f"{ERROR_NAMES.get(code, 'UNKNOWN')}({code})"


class StreamEngine:
    def __init__(self, dll_path: Optional[str] = None):
        path = Path(dll_path) if dll_path else next((p for p in default_dll_candidates() if p.is_file()), None)
        if path is None or not path.is_file():
            raise FileNotFoundError(
                "找不到 tobii_stream_engine.dll。請安裝 Tobii Ghost / Tobii Experience，"
                "或以 --dll 或環境變數 TOBII_STREAM_ENGINE_DLL 指定路徑。")
        self.dll_path = path
        self.dll = C.CDLL(str(path))
        d = self.dll
        d.tobii_api_create.argtypes = [C.POINTER(C.c_void_p), C.c_void_p, C.c_void_p]
        d.tobii_api_destroy.argtypes = [C.c_void_p]
        d.tobii_enumerate_local_device_urls.argtypes = [C.c_void_p, URL_CB, C.c_void_p]
        d.tobii_device_create.argtypes = [C.c_void_p, C.c_char_p, C.c_int, C.POINTER(C.c_void_p)]
        d.tobii_device_destroy.argtypes = [C.c_void_p]
        d.tobii_gaze_point_subscribe.argtypes = [C.c_void_p, GAZE_CB, C.c_void_p]
        d.tobii_gaze_point_unsubscribe.argtypes = [C.c_void_p]
        d.tobii_wait_for_callbacks.argtypes = [C.c_int, C.POINTER(C.c_void_p)]
        d.tobii_device_process_callbacks.argtypes = [C.c_void_p]
        d.tobii_device_reconnect.argtypes = [C.c_void_p]
        d.tobii_system_clock.argtypes = [C.c_void_p, C.POINTER(C.c_int64)]
        self.api = C.c_void_p()
        rc = d.tobii_api_create(C.byref(self.api), None, None)
        if rc != 0:
            raise RuntimeError(f"tobii_api_create 失敗：{_err(rc)}")

    def list_devices(self) -> list[str]:
        urls: list[str] = []
        cb = URL_CB(lambda u, _: urls.append(u.decode("utf-8", "replace")))
        rc = self.dll.tobii_enumerate_local_device_urls(self.api, cb, None)
        if rc != 0:
            raise RuntimeError(f"tobii_enumerate_local_device_urls 失敗：{_err(rc)}")
        return urls

    def system_clock_us(self) -> int:
        v = C.c_int64()
        rc = self.dll.tobii_system_clock(self.api, C.byref(v))
        if rc != 0:
            raise RuntimeError(f"tobii_system_clock 失敗：{_err(rc)}")
        return v.value

    def close(self):
        if self.api:
            self.dll.tobii_api_destroy(self.api)
            self.api = C.c_void_p()


class ClockSync:
    """把 Tobii 系統時鐘（µs）換成 Unix epoch 毫秒。

    與 Ghost 的 TrackerController.Create 相同的手法：
    取 UtcNow → tobii_system_clock → UtcNow，以兩次 UTC 的中點對應該時鐘值。
    """

    def __init__(self, engine: StreamEngine):
        self.engine = engine
        self.offset_us = 0.0
        self.last_sync = 0.0
        self.sync()

    def sync(self):
        best = None
        for _ in range(5):  # 取往返時間最短的一次
            t0 = time.time()
            sc = self.engine.system_clock_us()
            t1 = time.time()
            rtt = t1 - t0
            if best is None or rtt < best[0]:
                best = (rtt, (t0 + t1) / 2 * 1e6 - sc)
        self.offset_us = best[1]
        self.last_sync = time.time()

    def to_epoch_ms(self, ts_us: int) -> float:
        return (ts_us + self.offset_us) / 1000.0


class TobiiGazeSource(GazeSource):
    name = "tobii"

    def __init__(self, dll_path: Optional[str] = None, device_url: Optional[str] = None,
                 field_of_use: str = "analytical"):
        self.engine = StreamEngine(dll_path)
        urls = self.engine.list_devices()
        if device_url is None:
            if not urls:
                self.engine.close()
                raise RuntimeError(
                    "Stream Engine 找不到任何眼動儀。請確認眼動儀已接上、Tobii Experience 服務在執行，"
                    "並已完成顯示器設定與校正。")
            device_url = urls[0]
        self.device_url = device_url
        self.field_of_use = field_of_use
        self.device = C.c_void_p()
        rc = self.engine.dll.tobii_device_create(
            self.engine.api, device_url.encode(), FIELD_OF_USE[field_of_use], C.byref(self.device))
        if rc != 0:
            self.engine.close()
            hint = ""
            if field_of_use == "analytical":
                hint = ("\n此裝置拒絕 analytical（儲存/分析）用途。消費級眼動儀（如 Eye Tracker 5）"
                        "記錄視線資料需要 Tobii 的分析用途授權；若你確定有權使用，可改用 --field-of-use interactive。")
            raise RuntimeError(f"tobii_device_create 失敗：{_err(rc)}{hint}")
        self.clock = ClockSync(self.engine)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cb_ref = None
        self._on_sample: Optional[Callable[[GazeSample], None]] = None

    def _gaze_cb(self, p, _user):
        g = p.contents
        valid = g.validity == TOBII_VALIDITY_VALID
        sample = GazeSample(
            t_ms=self.clock.to_epoch_ms(g.timestamp_us),
            device_ts_us=g.timestamp_us,
            valid=valid,
            nx=float(g.position_xy[0]) if valid else float("nan"),
            ny=float(g.position_xy[1]) if valid else float("nan"),
        )
        if self._on_sample:
            self._on_sample(sample)

    def start(self, on_sample: Callable[[GazeSample], None]):
        self._on_sample = on_sample
        self._cb_ref = GAZE_CB(self._gaze_cb)  # 必須保留參照，避免被 GC
        rc = self.engine.dll.tobii_gaze_point_subscribe(self.device, self._cb_ref, None)
        if rc != 0:
            raise RuntimeError(f"tobii_gaze_point_subscribe 失敗：{_err(rc)}")
        self._thread = threading.Thread(target=self._loop, name="TobiiCallbackThread", daemon=True)
        self._thread.start()

    def _loop(self):
        d = self.engine.dll
        devs = (C.c_void_p * 1)(self.device)
        while not self._stop.is_set():
            rc = d.tobii_wait_for_callbacks(1, devs)
            if rc not in (0, ERR_TIMED_OUT):
                time.sleep(0.05)
            rc = d.tobii_device_process_callbacks(self.device)
            if rc in (ERR_CONNECTION_FAILED, ERR_CONNECTION_FAILED_DRIVER):
                print("[tobii] 連線中斷，嘗試重新連線…")
                while not self._stop.is_set() and d.tobii_device_reconnect(self.device) != 0:
                    time.sleep(0.5)
            if time.time() - self.clock.last_sync > 30:
                try:
                    self.clock.sync()
                except RuntimeError:
                    pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        d = self.engine.dll
        if self.device:
            d.tobii_gaze_point_unsubscribe(self.device)
            d.tobii_device_destroy(self.device)
            self.device = C.c_void_p()
        self.engine.close()

    def describe(self) -> dict:
        return {"source": "tobii", "device_url": self.device_url, "dll": str(self.engine.dll_path),
                "field_of_use": self.field_of_use}
