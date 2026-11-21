"""視線資料來源：Tobii 眼動儀，或用滑鼠模擬（沒有眼動儀時用來測試整條流程）。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import win32


@dataclass
class GazeSample:
    t_ms: float          # Unix epoch 毫秒（與瀏覽器 Date.now() 同一時間軸）
    device_ts_us: int    # 裝置原始時間戳（Tobii 系統時鐘 µs；模擬來源為 perf_counter µs）
    valid: bool
    nx: float            # 顯示器正規化座標 0..1（Tobii gaze_point 原生格式）
    ny: float


class GazeSource:
    name = "base"

    def start(self, on_sample: Callable[[GazeSample], None]):
        raise NotImplementedError

    def stop(self):
        pass

    def describe(self) -> dict:
        return {"source": self.name}


class MouseGazeSource(GazeSource):
    """以滑鼠游標位置模擬 gaze point，輸出格式與 Tobii 相同（正規化到所選顯示器）。

    對應 Ghost 裡的 GeneratedPointProducer：沒有真實眼動儀時產生點位。
    """

    name = "mouse"

    def __init__(self, monitor: win32.Monitor, hz: float = 90.0):
        self.monitor = monitor
        self.period = 1.0 / hz
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, on_sample):
        def loop():
            m = self.monitor
            nxt = time.perf_counter()
            while not self._stop.is_set():
                x, y = win32.cursor_pos()
                on_sample(GazeSample(
                    t_ms=time.time() * 1000.0,
                    device_ts_us=int(time.perf_counter() * 1e6),
                    valid=True,
                    nx=(x - m.left) / m.width,
                    ny=(y - m.top) / m.height,
                ))
                nxt += self.period
                time.sleep(max(0.0, nxt - time.perf_counter()))
        self._thread = threading.Thread(target=loop, name="MouseGazeThread", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def describe(self):
        return {"source": "mouse-simulator", "hz": round(1 / self.period)}
