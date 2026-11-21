"""攝影機視線估計（沒有 Tobii 時的替代來源）。

流程：攝影機影像 → MediaPipe Face Landmarker（478 點，含虹膜 468–477 與頭部姿態矩陣）
→ 每眼「虹膜相對眼角」位置 + 眼瞼開合 + 頭部朝向/位置 = 特徵向量
→ 校正時收集「特徵 ↔ 螢幕點」配對，以脊迴歸（含虹膜二次項）擬合
→ 執行時預測正規化螢幕座標，經 One Euro 濾波平滑，輸出和 Tobii gaze point 相同格式。

精度預期：一般筆電鏡頭約 2–5° 視角（在 60 cm 距離約 2–5 cm），比 Tobii（約 0.5–1°）差很多；
頭部大幅移動後精度會下降，可用重新校正或「點擊校正」改善。
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .sources import GazeSample, GazeSource

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "gazeweb"
MODEL_PATH = DATA_DIR / "models" / "face_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
CALIB_PATH = DATA_DIR / "webcam_calibration.json"

# MediaPipe Face Mesh 索引（影像中的左右）
R_OUT, R_IN, R_TOP, R_BOT = 33, 133, 159, 145
L_IN, L_OUT, L_TOP, L_BOT = 362, 263, 386, 374
R_IRIS, L_IRIS = range(468, 473), range(473, 478)

FEATURE_NAMES = ["r_eye_x", "r_eye_y", "l_eye_x", "l_eye_y", "openness",
                 "head_fwd_x", "head_fwd_y", "head_roll", "head_tx", "head_ty", "head_tz"]


def ensure_model() -> Path:
    if not MODEL_PATH.is_file():
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        print(f"下載 MediaPipe 臉部模型（約 3.6 MB）：{MODEL_URL}")
        tmp = MODEL_PATH.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.replace(MODEL_PATH)
    return MODEL_PATH


def blink_mask(openness: np.ndarray, ratio: float = 0.85, pad: int = 1) -> np.ndarray:
    """回傳「保留」遮罩：去掉眨眼（含半眨眼）畫面及其前後各 pad 格。

    基準用 75 百分位而非中位數：一段資料裡眨眼畫面可能占一半，中位數會被拉低。
    往下看時眼瞼本來就會變窄，所以只在同一個注視點內比較，不跨點比。
    """
    if len(openness) == 0:
        return np.zeros(0, bool)
    bad = openness < ratio * np.percentile(openness, 75)
    if pad:
        b = bad.copy()
        for k in range(1, pad + 1):
            b[k:] |= bad[:-k]
            b[:-k] |= bad[k:]
        bad = b
    return ~bad


class BlinkFilter:
    """即時版：和最近 0.6 秒的開合基準比較，眨眼時與之後 100 ms 視為無效。"""

    def __init__(self, ratio: float = 0.8, window_ms: float = 600, hold_ms: float = 100):
        self.ratio, self.window_ms, self.hold_ms = ratio, window_ms, hold_ms
        self.hist: deque[tuple[float, float]] = deque()
        self.until = 0.0

    def __call__(self, t_ms: float, openness: float) -> bool:
        """True = 正在眨眼（樣本無效）。"""
        self.hist.append((t_ms, openness))
        while self.hist and t_ms - self.hist[0][0] > self.window_ms:
            self.hist.popleft()
        base = float(np.percentile([o for _, o in self.hist], 75))
        if openness < self.ratio * base:
            self.until = t_ms + self.hold_ms
        return t_ms < self.until


@dataclass
class Features:
    t_ms: float
    vec: Optional[np.ndarray]   # None = 沒偵測到臉
    openness: float = 0.0
    face_box: Optional[tuple] = None  # 預覽用 (x0, y0, x1, y1)，正規化


def extract_features(lms, matrix, w: int, h: int) -> tuple[np.ndarray, float, tuple]:
    P = np.array([[p.x * w, p.y * h] for p in lms], dtype=np.float64)

    def eye(a, b, top, bot, iris):
        o = (P[a] + P[b]) / 2
        u = P[b] - P[a]
        width = np.linalg.norm(u) + 1e-6
        u /= width
        v = np.array([-u[1], u[0]])
        c = P[list(iris)].mean(axis=0) - o
        return c @ u / width, c @ v / width, np.linalg.norm(P[top] - P[bot]) / width

    rx, ry, ro = eye(R_OUT, R_IN, R_TOP, R_BOT, R_IRIS)
    lx, ly, lo = eye(L_IN, L_OUT, L_TOP, L_BOT, L_IRIS)
    if matrix is not None:
        M = np.asarray(matrix).reshape(4, 4)
        fwd_x, fwd_y, roll = M[0, 2], M[1, 2], M[1, 0]
        tx, ty, tz = M[0, 3], M[1, 3], M[2, 3]
    else:
        fwd_x = fwd_y = roll = tx = ty = tz = 0.0
    xs, ys = P[:, 0] / w, P[:, 1] / h
    box = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
    openness = (ro + lo) / 2
    return np.array([rx, ry, lx, ly, openness, fwd_x, fwd_y, roll, tx, ty, tz]), openness, box


class FaceTracker:
    """背景執行緒：讀攝影機、跑 Face Landmarker，保留最近 3 秒的特徵供校正與點擊校正使用。"""

    def __init__(self, camera: int = 0, width: int = 1280, height: int = 720):
        import cv2  # noqa: F401  延後匯入，Tobii 使用者不需要 OpenCV
        self.camera, self.width, self.height = camera, width, height
        self.lock = threading.Lock()
        self.history: deque[Features] = deque(maxlen=150)
        self.preview = None
        self.fps = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listeners: list[Callable[[Features], None]] = []
        self.error: Optional[str] = None

    def add_listener(self, fn: Callable[[Features], None]):
        self._listeners.append(fn)

    def start(self):
        ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(ready,), name="FaceTrackerThread", daemon=True)
        self._thread.start()
        ready.wait(15)
        if self.error:
            raise RuntimeError(self.error)

    def _loop(self, ready: threading.Event):
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        cap = cv2.VideoCapture(self.camera, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        ok, frame = cap.read()
        if not ok:
            self.error = f"無法開啟攝影機 {self.camera}（被其他程式占用，或編號不對；可用 --camera 指定）"
            ready.set()
            return
        lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(ensure_model())),
            running_mode=vision.RunningMode.VIDEO, num_faces=1,
            output_facial_transformation_matrixes=True))
        ready.set()
        last_ts, n, t_fps = 0, 0, time.time()
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                t_ms = time.time() * 1000.0
                if not ok:
                    time.sleep(0.01)
                    continue
                ts = max(last_ts + 1, int(t_ms))
                last_ts = ts
                h, w = frame.shape[:2]
                res = lm.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), ts)
                if res.face_landmarks:
                    mats = res.facial_transformation_matrixes
                    vec, op, box = extract_features(res.face_landmarks[0], mats[0] if mats else None, w, h)
                    f = Features(t_ms, vec, op, box)
                else:
                    f = Features(t_ms, None)
                small = cv2.resize(frame, (480, int(480 * h / w)))
                with self.lock:
                    self.history.append(f)
                    self.preview = cv2.flip(small, 1)
                for fn in self._listeners:
                    fn(f)
                n += 1
                if time.time() - t_fps >= 1:
                    self.fps, n, t_fps = n / (time.time() - t_fps), 0, time.time()
        finally:
            lm.close()
            cap.release()

    def latest(self) -> Optional[Features]:
        with self.lock:
            return self.history[-1] if self.history else None

    def window(self, t0: float, t1: float) -> list[Features]:
        with self.lock:
            return [f for f in self.history if t0 <= f.t_ms <= t1 and f.vec is not None]

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


class GazeModel:
    """特徵 → 正規化螢幕座標，脊迴歸。

    眼睛項：平均虹膜 ex、ey、眼瞼開合 op，加上 ex²、ex³（虹膜水平位移在邊緣會壓縮）
    與 ex·ey（往下看時眼瞼遮住虹膜、鏡頭在上方，水平位移範圍變小）。
    這個結構是用實際校正資料比較多種組合選出的：驗證誤差 158 → 97 px（同一份資料）。
    頭部項（朝向、位置）另用較強的懲罰，避免校正時的微小頭動被過度解讀。
    兩個正則化強度用「留一個校正點」交叉驗證自動挑選。
    """

    VERSION = 2
    EYE_LAMBDAS = (0.01, 0.3, 3.0)
    HEAD_LAMBDAS = (3.0, 30.0)
    N_EYE_TERMS = 6
    # 各特徵標準差的下限（物理上合理的變動量）。校正時頭幾乎不動，頭部特徵的變異極小，
    # 若直接用來標準化，之後頭稍微一動就會被放大成巨大數值、預測爆掉。
    STD_FLOOR = np.array([0.01, 0.01, 0.01, 0.01, 0.02, 0.05, 0.05, 0.05, 1.5, 1.5, 3.0])

    def __init__(self):
        self.mean = self.std = self.W = None
        self.lam = (0.3, 3.0)  # (眼睛項, 頭部項)
        self.open_ref = 0.25
        self.use_cubic = True

    def _phi(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.mean) / self.std
        ex = (Z[:, 0] + Z[:, 2]) / 2
        ey = (Z[:, 1] + Z[:, 3]) / 2
        op = Z[:, 4]
        return np.column_stack([np.ones(len(Z)), ex, ey, op, ex * ex, ex * ey, ex ** 3, Z[:, 5:11]])

    def _penalty(self, lam) -> np.ndarray:
        p = np.array([0.0] + [lam[0]] * self.N_EYE_TERMS + [lam[1]] * 6)  # 不懲罰截距
        if not self.use_cubic:
            p[6] = 1e6  # 等於拿掉 ex³ 項
        return p

    def _solve(self, Phi, Y, w, lam):
        A = Phi.T @ (Phi * w[:, None])
        return np.linalg.solve(A + np.diag(self._penalty(lam)), Phi.T @ (Y * w[:, None]))

    def fit(self, X: np.ndarray, Y: np.ndarray, groups: np.ndarray, w: Optional[np.ndarray] = None):
        w = np.ones(len(X)) if w is None else w
        self.mean = X.mean(axis=0)
        self.std = np.maximum(X.std(axis=0), self.STD_FLOOR)
        self.open_ref = float(np.median(X[:, 4]))
        # 三次項需要至少 5 個不同的水平位置才約束得住；9 點校正只有 3 個（0.1/0.5/0.9），
        # 硬擬三次曲線會在點與點之間大幅擺盪
        self.use_cubic = len(np.unique(np.round(Y[:, 0], 2))) >= 5
        Phi = self._phi(X)
        uniq = np.unique(groups)
        if len(uniq) >= 5:
            best = None
            for lam in [(a, b) for a in self.EYE_LAMBDAS for b in self.HEAD_LAMBDAS]:
                err = 0.0
                for g in uniq:
                    tr, te = groups != g, groups == g
                    Wg = self._solve(Phi[tr], Y[tr], w[tr], lam)
                    # 以每點的中位數預測計算誤差（和驗證畫面的算法一致）
                    err += np.linalg.norm(np.median(Phi[te] @ Wg, axis=0) - Y[te][0])
                if best is None or err < best[0]:
                    best = (err, lam)
            self.lam = best[1]
        self.W = self._solve(Phi, Y, w, self.lam)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return (self._phi(np.atleast_2d(x)) @ self.W)

    def head_z(self, x: np.ndarray) -> float:
        """頭部特徵（朝向、旋轉、位置）離校正時平均值最多幾個標準差。"""
        return float(np.max(np.abs((x[5:11] - self.mean[5:11]) / self.std[5:11])))

    def to_dict(self):
        return {"version": self.VERSION, "mean": self.mean.tolist(), "std": self.std.tolist(),
                "W": self.W.tolist(), "lam": list(self.lam), "open_ref": self.open_ref,
                "use_cubic": self.use_cubic, "features": FEATURE_NAMES}

    @classmethod
    def from_dict(cls, d):
        if d.get("version") != cls.VERSION:
            raise ValueError("舊版模型格式")
        m = cls()
        m.mean, m.std, m.W = np.array(d["mean"]), np.array(d["std"]), np.array(d["W"])
        m.lam, m.open_ref = tuple(d["lam"]), d["open_ref"]
        m.use_cubic = d.get("use_cubic", True)
        return m


class OneEuro:
    """One Euro 濾波：注視時強力平滑、快速掃視時降低延遲。"""

    def __init__(self, min_cutoff=0.8, beta=4.0, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = None
        self.t = None

    @staticmethod
    def _a(cutoff, dt):
        r = 2 * math.pi * cutoff * dt
        return r / (r + 1)

    def __call__(self, x: np.ndarray, t: float) -> np.ndarray:
        if self.x is None or t - self.t > 0.5:
            self.x, self.dx, self.t = x, np.zeros_like(x), t
            return x
        dt = max(1e-3, t - self.t)
        dx = (x - self.x) / dt
        self.dx = self.dx + self._a(self.d_cutoff, dt) * (dx - self.dx)
        cutoff = self.min_cutoff + self.beta * np.linalg.norm(self.dx)
        self.x = self.x + self._a(cutoff, dt) * (x - self.x)
        self.t = t
        return self.x


@dataclass
class Calibration:
    X: np.ndarray            # 特徵樣本
    Y: np.ndarray            # 對應的正規化螢幕座標
    groups: np.ndarray       # 校正點編號（留一交叉驗證用）
    model: GazeModel
    report: dict

    def save(self, path: Path = CALIB_PATH, **extra):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), **extra, "report": self.report,
            "blink_filtered": True,
            "model": self.model.to_dict(), "X": self.X.tolist(), "Y": self.Y.tolist(),
            "groups": self.groups.tolist()}, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = CALIB_PATH) -> "Calibration":
        d = json.loads(path.read_text(encoding="utf-8"))
        X, Y, G = np.array(d["X"]), np.array(d["Y"]), np.array(d["groups"])
        try:
            if not d.get("blink_filtered"):
                raise ValueError("未過濾眨眼")
            model = GazeModel.from_dict(d["model"])
        except ValueError:  # 舊版存檔：過濾眨眼後，用保存的原始樣本以新模型重新擬合
            keep = np.concatenate([blink_mask(X[G == g, 4]) for g in dict.fromkeys(G.tolist())])
            order = np.concatenate([np.where(G == g)[0] for g in dict.fromkeys(G.tolist())])
            idx = np.sort(order[keep])
            X, Y, G = X[idx], Y[idx], G[idx]
            model = GazeModel().fit(X, Y, G)
        return cls(X, Y, G, model, d["report"])


class WebcamGazeSource(GazeSource):
    name = "webcam"
    HEAD_Z_MAX = 6.0  # 頭部特徵離校正平均幾個（下限化的）標準差以上就不信任

    def __init__(self, tracker: FaceTracker, calib: Calibration, click_calibration: bool = True):
        self.tracker = tracker
        self.calib = calib
        self.model = calib.model
        self.click_calibration = click_calibration
        self.filter = OneEuro()
        self.blink = BlinkFilter()
        self.rejected = {"blink_or_no_face": 0, "head_out_of_range": 0, "implausible": 0}
        self.lock = threading.Lock()
        self._on_sample = None
        self._running = False
        self.click_points = 0
        self.click_skipped = {"no_valid_frame": 0, "too_far": 0}
        self.offset = np.zeros(2)          # 定位檢查＋點擊並行估出的整體偏移（正規化螢幕座標）
        self._refs: deque[tuple[float, np.ndarray, float]] = deque(maxlen=25)  # (時間, 殘差, 權重)
        self.seed_points = 0
        tracker.add_listener(self._on_features)

    def _on_features(self, f: Features):
        if not self._running or not self._on_sample:
            return
        with self.lock:
            model, offset = self.model, self.offset.copy()
        invalid = GazeSample(f.t_ms, int(f.t_ms * 1000), False, float("nan"), float("nan"))
        blinking = f.vec is not None and self.blink(f.t_ms, f.openness)
        if f.vec is None or blinking or f.openness < 0.5 * model.open_ref:  # 沒臉或眨眼
            self.rejected["blink_or_no_face"] += 1
            self._on_sample(invalid)
            return
        if model.head_z(f.vec) > self.HEAD_Z_MAX:
            # 頭部朝向/位置遠超出校正時的範圍（轉頭、低頭看鍵盤、臉部移出畫面邊緣），
            # 模型只能外插，實測會產生 y = -38 這類離譜值，直接標為無效
            self.rejected["head_out_of_range"] += 1
            self._on_sample(invalid)
            return
        p = model.predict(f.vec)[0] + offset
        if not (-0.5 <= p[0] <= 1.5 and -0.5 <= p[1] <= 1.5):
            # 超出螢幕半個螢幕以上：物理上不合理，視為追蹤失敗
            # （略超出螢幕仍保留，例如看旁邊的第二個螢幕時 x ≈ 1.1–1.2）
            self.rejected["implausible"] += 1
            self._on_sample(invalid)
            return
        p = self.filter(p, f.t_ms / 1000.0)
        self._on_sample(GazeSample(f.t_ms, int(f.t_ms * 1000), True, float(p[0]), float(p[1])))

    CLICK_MAX_RESIDUAL = 0.35   # 點擊位置與預測差超過螢幕的 35%：多半沒在看點擊處，不採用
    CLICK_MAX_OFFSET = 0.20     # 偏移修正上限
    CLICK_HALF_LIFE_S = 60.0    # 舊點擊的權重半衰期，跟上坐姿的緩慢變化

    def add_reference(self, t_ms: float, nx: float, ny: float):
        """點擊校正（漂移修正）：使用者點擊時通常正看著點擊位置。

        不重新擬合整個模型（實測少量、集中在同一點的點擊會讓模型失控），而是：
        點擊前 300 ms 的原始預測中位數 vs 點擊位置 → 殘差；差太多的點擊捨棄；
        以時間衰減加權、去除離群後的平均殘差當作整體偏移，並限制大小。
        """
        if not self.click_calibration or not (0 <= nx <= 1 and 0 <= ny <= 1):
            return
        model = self.model
        preds = [model.predict(f.vec)[0] for f in self.tracker.window(t_ms - 300, t_ms + 50)
                 if f.openness >= 0.6 * model.open_ref and model.head_z(f.vec) <= self.HEAD_Z_MAX]
        if not preds:
            self.click_skipped["no_valid_frame"] += 1
            return
        r = np.array([nx, ny]) - np.median(np.array(preds), axis=0)
        if np.linalg.norm(r) > self.CLICK_MAX_RESIDUAL:
            self.click_skipped["too_far"] += 1
            return
        with self.lock:
            self._refs.append((t_ms, r, 1.0))
            self._recompute(t_ms)
            self.click_points += 1

    SEED_WEIGHT = 2.0  # 開始時定位檢查的點（刻意注視、品質較好）權重是點擊的 2 倍

    def seed_offset(self, residuals: list, t_ms: Optional[float] = None):
        """錄製開始前定位檢查得到的殘差（目標 − 預測）作為初始偏移；之後的點擊會一起並行修正。"""
        t_ms = t_ms if t_ms is not None else time.time() * 1000.0
        with self.lock:
            for r in residuals:
                self._refs.append((t_ms, np.asarray(r, dtype=float), self.SEED_WEIGHT))
            self._recompute(t_ms)
            self.seed_points = len(residuals)

    def _recompute(self, now_ms: float):
        """把定位點與點擊點一起估計整體偏移：時間衰減加權、去除離群、樣本少時往 0 收縮、限制大小。"""
        R = np.array([x for _, x, _ in self._refs])
        base = np.array([w for _, _, w in self._refs])
        w = base * np.array([0.5 ** ((now_ms - t) / 1000 / self.CLICK_HALF_LIFE_S) for t, _, _ in self._refs])
        if len(R) >= 3:  # 去掉離中位數最遠的那些（例如連點時視線已移開）
            d = np.linalg.norm(R - np.median(R, axis=0), axis=1)
            w = w * (d <= max(0.05, 2.5 * np.median(d)))
        est = (R * w[:, None]).sum(axis=0) / max(w.sum(), 1e-9)
        n_eff = base.sum()
        shrink = n_eff / (n_eff + 2.0)
        self.offset = np.clip(est * shrink, -self.CLICK_MAX_OFFSET, self.CLICK_MAX_OFFSET)

    def start(self, on_sample):
        self._on_sample = on_sample
        self._running = True

    def stop(self):
        self._running = False
        self.tracker.stop()

    def describe(self):
        return {"source": "webcam", "camera": self.tracker.camera, "calibration": self.calib.report,
                "positioning_points": self.seed_points,
                "click_calibration": self.click_calibration, "click_points_used": self.click_points,
                "click_skipped": self.click_skipped, "final_offset": [round(float(v), 4) for v in self.offset],
                "rejected_samples": self.rejected,
                "note": "攝影機視線估計，精度遠低於 Tobii，僅供流程測試與粗略分析"}
