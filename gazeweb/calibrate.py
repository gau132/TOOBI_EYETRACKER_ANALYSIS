"""攝影機視線的全螢幕校正流程：
1. 位置調整：顯示鏡頭畫面與提示，直到臉部位置、距離合適（按空白鍵開始）
2. 校正：圓點依序移到 9 或 13 個位置，每點收集「看著圓點」時的特徵
3. 擬合模型
4. 驗證：另外 5 個點，量測實際誤差（像素 / 公分 / 視角度數）
5. 結果：Enter 接受、R 重做、Esc 取消
另有 live_test()：全螢幕即時顯示估計的視線點，檢查準不準。
"""
from __future__ import annotations

import ctypes as C
import math
import time
from typing import Optional

import numpy as np

from . import win32
from .webcam import BlinkFilter, Calibration, FaceTracker, GazeModel, OneEuro, WebcamGazeSource, blink_mask

WIN = "gazeweb-calibration"
BG = (32, 30, 28)          # BGR
FG = (235, 235, 230)
MUTED = (150, 150, 145)
ACCENT = (46, 87, 228)     # 橘紅 (BGR)
GOOD = (90, 180, 60)
BLUE = (255, 98, 41)

CALIB_13 = [(.1, .1), (.5, .1), (.9, .1), (.1, .5), (.5, .5), (.9, .5), (.1, .9), (.5, .9), (.9, .9),
            (.3, .3), (.7, .3), (.3, .7), (.7, .7)]
CALIB_9 = CALIB_13[:9]
VALID_5 = [(.25, .2), (.75, .2), (.5, .5), (.2, .8), (.8, .8)]


def _font(size):
    from PIL import ImageFont
    for name in ("msjh.ttc", "msjhl.ttc", "mingliu.ttc", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def monitor_size_mm(mon: win32.Monitor) -> Optional[tuple[float, float]]:
    """從顯示器 DC 取得實體尺寸（mm），用來把像素誤差換成公分與視角。"""
    try:
        gdi = C.WinDLL("gdi32")
        gdi.CreateDCW.restype = C.c_void_p
        gdi.CreateDCW.argtypes = [C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p]
        gdi.GetDeviceCaps.argtypes = [C.c_void_p, C.c_int]
        gdi.DeleteDC.argtypes = [C.c_void_p]
        dc = gdi.CreateDCW(mon.name, None, None, None)
        w, h = gdi.GetDeviceCaps(dc, 4), gdi.GetDeviceCaps(dc, 6)  # HORZSIZE, VERTSIZE
        gdi.DeleteDC(dc)
        return (float(w), float(h)) if w > 0 and h > 0 else None
    except Exception:  # noqa: BLE001
        return None


class Screen:
    """在指定顯示器上的全螢幕 OpenCV 視窗，文字用 Pillow 畫（支援中文）。"""

    def __init__(self, mon: win32.Monitor):
        import cv2
        self.cv2 = cv2
        self.mon = mon
        self.W, self.H = mon.width, mon.height
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.moveWindow(WIN, mon.left, mon.top)
        cv2.resizeWindow(WIN, self.W, self.H)
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        self.scale = self.H / 1080
        self._fonts = {}

    def blank(self):
        img = np.empty((self.H, self.W, 3), np.uint8)
        img[:] = BG
        return img

    def text(self, img, lines, y=None, size=34, color=FG, center=True, x=None):
        """畫多行文字（Pillow，只轉換用到的區域以節省時間）。"""
        from PIL import Image, ImageDraw
        px = int(size * self.scale)
        f = self._fonts.get(px) or self._fonts.setdefault(px, _font(px))
        lines = [lines] if isinstance(lines, str) else lines
        lh = int(px * 1.5)
        y = int(self.H * 0.42) if y is None else y
        top, bottom = max(0, y - 4), min(self.H, y + lh * len(lines) + 8)
        if bottom <= top:
            return
        region = Image.fromarray(img[top:bottom, :, ::-1].copy())
        d = ImageDraw.Draw(region)
        for i, line in enumerate(lines):
            w = d.textlength(line, font=f)
            lx = (self.W - w) / 2 if center else (x or 0)
            d.text((lx, 4 + i * lh), line, font=f, fill=color[::-1])
        img[top:bottom] = np.asarray(region)[:, :, ::-1]

    def show(self, img, wait=1) -> int:
        self.cv2.imshow(WIN, img)
        return self.cv2.waitKey(wait) & 0xFF

    def px(self, p):
        return int(p[0] * self.W), int(p[1] * self.H)

    def close(self):
        try:
            self.cv2.destroyWindow(WIN)
            self.cv2.waitKey(1)
        except Exception:  # noqa: BLE001
            pass


class CalibrationUI:
    def __init__(self, tracker: FaceTracker, mon: win32.Monitor, points: int = 13,
                 distance_cm: float = 60.0, auto: bool = False):
        self.tracker = tracker
        self.mon = mon
        self.targets = CALIB_13 if points >= 13 else CALIB_9
        self.distance_cm = distance_cm
        self.auto = auto            # 自動化測試用：不等按鍵
        self.current_target = None  # 測試用的假追蹤器會讀這個值
        self.mm = monitor_size_mm(mon)

    # ---------- 1. 位置調整 ----------
    def _position(self, s: Screen, title: str = "攝影機視線校正", info: Optional[list] = None) -> bool:
        cv2 = s.cv2
        info = info or ["校正共 %d 點，之後有 5 點驗證，約 40 秒。" % len(self.targets),
                        "提示：戴眼鏡可能降低精度；避免背光；校正後盡量維持相同坐姿。",
                        "空白鍵：開始　Esc：取消"]
        t_ok = None
        while True:
            img = s.blank()
            f = self.tracker.latest()
            prev = self.tracker.preview
            ok_face = f is not None and f.vec is not None
            msg, color = "找不到臉部：請面向螢幕，確認鏡頭沒被遮住、光線充足", ACCENT
            if prev is not None:
                ph, pw = prev.shape[:2]
                k = (s.H * 0.42) / ph
                pv = cv2.resize(prev, (int(pw * k), int(ph * k)))
                x0, y0 = (s.W - pv.shape[1]) // 2, int(s.H * 0.12)
                if ok_face:
                    bx0, by0, bx1, by1 = f.face_box
                    bx0, bx1 = 1 - bx1, 1 - bx0  # 預覽是鏡像
                    cv2.rectangle(pv, (int(bx0 * pv.shape[1]), int(by0 * pv.shape[0])),
                                  (int(bx1 * pv.shape[1]), int(by1 * pv.shape[0])), GOOD, 2)
                    width = bx1 - bx0
                    cx = (bx0 + bx1) / 2
                    if width < 0.18:
                        msg = "太遠了：請靠近螢幕一點（約一個手臂長，50–70 cm）"
                    elif width > 0.45:
                        msg = "太近了：請往後一點"
                    elif abs(cx - 0.5) > 0.15:
                        msg = "請把臉移到畫面中央"
                    else:
                        msg, color = "位置很好。校正時頭盡量不動，只用眼睛看圓點。按空白鍵開始", GOOD
                s_img = img[y0:y0 + pv.shape[0], x0:x0 + pv.shape[1]]
                s_img[:] = pv
            s.text(img, title, y=int(s.H * 0.03), size=40)
            s.text(img, msg, y=int(s.H * 0.58), size=32, color=color)
            s.text(img, info, y=int(s.H * 0.68), size=26, color=MUTED)
            key = s.show(img, 15)
            if key == 27:
                return False
            good = color == GOOD
            if self.auto and ok_face:
                good = True
            if good and (key == 32 or self.auto):
                t_ok = t_ok or time.time()
                if not self.auto or time.time() - t_ok > 0.3:
                    return True

    # ---------- 2/4. 收集 ----------
    def _collect(self, s: Screen, targets, title: str):
        """回傳 [(target, [Features...]), ...]；Esc 回傳 None。"""
        cv2 = s.cv2
        out = []
        prev = (0.5, 0.5)
        move_s, settle_s, collect_s = 0.55, 0.45, 0.9
        for i, tgt in enumerate(targets):
            t0 = time.time()
            feats = []
            collect_from = None
            while True:
                t = time.time() - t0
                if t < move_s:  # 平滑移動到下一點，引導視線
                    k = 0.5 - 0.5 * math.cos(math.pi * t / move_s)
                    p = (prev[0] + (tgt[0] - prev[0]) * k, prev[1] + (tgt[1] - prev[1]) * k)
                    r = 18
                else:
                    p = tgt
                    k = min(1.0, (t - move_s) / (settle_s + collect_s))
                    r = int(30 - 22 * k)
                self.current_target = p
                img = s.blank()
                s.text(img, f"{title}　{i + 1} / {len(targets)}　請注視圓點中心", y=int(s.H * 0.02), size=24, color=MUTED)
                c = s.px(p)
                rr = int(r * s.scale)
                cv2.circle(img, c, rr, ACCENT, -1, cv2.LINE_AA)
                cv2.circle(img, c, max(2, int(4 * s.scale)), FG, -1, cv2.LINE_AA)
                f = self.tracker.latest()
                if f is None or f.vec is None:
                    s.text(img, "偵測不到臉部，請面向螢幕", y=int(s.H * 0.94), size=26, color=ACCENT)
                key = s.show(img, 1)
                if key == 27:
                    return None
                if t >= move_s + settle_s:
                    collect_from = collect_from or (time.time() * 1000)
                if t >= move_s + settle_s + collect_s:
                    feats = self.tracker.window(collect_from, time.time() * 1000)
                    if len(feats) >= 8:
                        break
                    if t > move_s + settle_s + collect_s + 4:  # 這點一直抓不到臉：跳過
                        break
            prev = tgt
            if feats:
                # 先去掉眨眼畫面（開合度驟降，會把預測拉到螢幕外）
                feats = [f for f, k in zip(feats, blink_mask(np.array([f.openness for f in feats]))) if k]
            if len(feats) >= 3:
                # 再丟掉虹膜位置離中位數太遠的樣本（視線偏移）
                V = np.array([f.vec for f in feats])
                d = np.linalg.norm((V[:, :4] - np.median(V[:, :4], axis=0)), axis=1)
                keep = d <= np.percentile(d, 80) + 1e-9
                out.append((tgt, [f for f, k in zip(feats, keep) if k]))
        self.current_target = None
        return out

    # ---------- 5. 結果 ----------
    def _px_per_cm(self):
        if not self.mm:
            return None
        return self.mon.width / (self.mm[0] / 10.0)

    def _evaluate(self, model: GazeModel, data) -> dict:
        errs, per = [], []
        for tgt, feats in data:
            if not feats:
                continue
            P = model.predict(np.array([f.vec for f in feats]))
            est = np.median(P, axis=0)
            e_px = math.hypot((est[0] - tgt[0]) * self.mon.width, (est[1] - tgt[1]) * self.mon.height)
            sd_px = float(np.mean(np.linalg.norm((P - est) * [self.mon.width, self.mon.height], axis=1)))
            errs.append(e_px)
            per.append({"target": tgt, "estimate": [float(est[0]), float(est[1])], "error_px": round(e_px, 1),
                        "precision_px": round(sd_px, 1)})
        rep = {"mean_error_px": round(float(np.mean(errs)), 1) if errs else None,
               "max_error_px": round(float(np.max(errs)), 1) if errs else None,
               "points": per}
        ppc = self._px_per_cm()
        if ppc and errs:
            cm = float(np.mean(errs)) / ppc
            rep["mean_error_cm"] = round(cm, 2)
            rep["mean_error_deg"] = round(math.degrees(math.atan2(cm, self.distance_cm)), 2)
            rep["assumed_distance_cm"] = self.distance_cm
        return rep

    def _result(self, s: Screen, rep: dict) -> int:
        cv2 = s.cv2
        t0 = time.time()
        while True:
            img = s.blank()
            for p in rep["points"]:
                a, b = s.px(p["target"]), s.px(p["estimate"])
                cv2.line(img, a, b, MUTED, 2, cv2.LINE_AA)
                cv2.circle(img, a, int(10 * s.scale), FG, 2, cv2.LINE_AA)
                cv2.circle(img, b, max(4, int(p["precision_px"])), BLUE, 2, cv2.LINE_AA)
                cv2.circle(img, b, int(5 * s.scale), BLUE, -1, cv2.LINE_AA)
            e = rep["mean_error_px"]
            if e is None:
                lines = ["驗證時偵測不到臉部，無法計算誤差。"]
            else:
                pct = 100 * e / self.mon.width
                lines = [f"平均誤差 {e:.0f} px（螢幕寬度的 {pct:.1f}%）"]
                if "mean_error_deg" in rep:
                    lines[0] += f"，約 {rep['mean_error_cm']:.1f} cm / {rep['mean_error_deg']:.1f}°"
                grade = ("很好" if pct < 4 else "可用" if pct < 7 else "偏差較大，建議重做（調整光線、坐姿，頭保持不動）")
                lines.append("評估：" + grade)
            # 文字放在驗證點（y = 0.2 / 0.5 / 0.8）之間的空白帶
            s.text(img, "驗證結果", y=int(s.H * 0.56), size=40)
            s.text(img, lines, y=int(s.H * 0.62), size=30)
            s.text(img, ["白圈＝目標位置，藍點＝估計位置（圈大小＝抖動程度）",
                         "Enter：接受並開始　R：重新校正　Esc：取消"], y=int(s.H * 0.86), size=24, color=MUTED)
            key = s.show(img, 30)
            if self.auto and time.time() - t0 > 1.0:
                return 13
            if key in (13, 27, ord("r"), ord("R")):
                return key

    def quick_check(self, calib: Calibration):
        """錄製前定位：沿用既有校正模型，看 5 個點量出目前的整體偏差（坐姿、頭部位置的變化）。

        回傳 ("ok", 殘差清單, 報告)、("full", [], 報告)（需要完整校正）或 None（取消）。
        殘差會交給 WebcamGazeSource.seed_offset 當初始偏移，之後錄製中的點擊再並行修正。
        """
        s = Screen(self.mon)
        W, H = self.mon.width, self.mon.height
        try:
            ok = self._position(s, "錄製前定位", [
                "沿用最近的校正，只需看 5 個點（約 10 秒）量出目前的偏差；錄製中的點擊會再持續修正。",
                "請維持接下來瀏覽時的坐姿。",
                "空白鍵：開始　Esc：取消"])
            if not ok:
                return None
            data = self._collect(s, VALID_5, "定位")
            if data is None:
                return None
            pts = []
            for tgt, feats in data:
                if len(feats) >= 3:
                    est = np.median(calib.model.predict(np.array([f.vec for f in feats])), axis=0)
                    pts.append((np.array(tgt), est))
            if len(pts) < 3:
                img = s.blank()
                s.text(img, ["定位時常常偵測不到臉部，改做完整校正。", "按任意鍵繼續。"])
                s.show(img, 0 if not self.auto else 500)
                return ("full", [], {})
            R = np.array([t - e for t, e in pts])
            off = np.median(R, axis=0)
            px = lambda v: math.hypot(v[0] * W, v[1] * H)  # noqa: E731
            before = float(np.mean([px(r) for r in R]))
            after = float(np.mean([px(r - off) for r in R]))
            rep = {"positioning_before_px": round(before, 1), "positioning_after_px": round(after, 1),
                   "positioning_offset": [round(float(v), 4) for v in off]}
            need_full = after / W > 0.07
            t0 = time.time()
            while True:
                img = s.blank()
                for t, e in pts:
                    a, b, c = s.px(t), s.px(e), s.px(e + off)
                    s.cv2.line(img, a, b, MUTED, 1, s.cv2.LINE_AA)
                    s.cv2.circle(img, a, int(10 * s.scale), FG, 2, s.cv2.LINE_AA)
                    s.cv2.circle(img, b, int(5 * s.scale), BLUE, -1, s.cv2.LINE_AA)
                    s.cv2.circle(img, c, int(6 * s.scale), GOOD, -1, s.cv2.LINE_AA)
                s.text(img, "定位結果", y=int(s.H * 0.56), size=40)
                lines = [f"目前偏差 {before:.0f} px → 套用偏移修正後約 {after:.0f} px（螢幕寬度的 {100 * after / W:.1f}%）"]
                lines.append("建議重新做完整校正（坐姿或光線變化太大）" if need_full else "可以開始錄製；錄製中的點擊會繼續修正")
                s.text(img, lines, y=int(s.H * 0.62), size=30, color=ACCENT if need_full else FG)
                s.text(img, ["白圈＝目標，藍點＝原本的估計，綠點＝修正後的估計",
                             "Enter：開始錄製　F：完整校正　Esc：取消"], y=int(s.H * 0.86), size=24, color=MUTED)
                key = s.show(img, 30)
                if self.auto and time.time() - t0 > 1.0:
                    key = 13
                if key == 13:
                    return ("ok", list(R), rep)
                if key in (ord("f"), ord("F")):
                    return ("full", [], rep)
                if key == 27:
                    return None
        finally:
            s.close()

    def run(self) -> Optional[Calibration]:
        s = Screen(self.mon)
        try:
            while True:
                if not self._position(s):
                    return None
                data = self._collect(s, self.targets, "校正")
                if data is None:
                    return None
                data = [d for d in data if len(d[1]) >= 3]
                if len(data) < 6:
                    img = s.blank()
                    s.text(img, ["有效的校正點太少（臉部常常偵測不到）。", "請改善光線或位置後重試。按任意鍵繼續。"])
                    s.show(img, 0 if not self.auto else 500)
                    if self.auto:
                        return None
                    continue
                X = np.vstack([np.array([f.vec for f in fs]) for _, fs in data])
                Y = np.vstack([np.tile(t, (len(fs), 1)) for t, fs in data])
                G = np.concatenate([np.full(len(fs), i) for i, (_, fs) in enumerate(data)])
                model = GazeModel().fit(X, Y, G)
                val = self._collect(s, VALID_5, "驗證")
                if val is None:
                    return None
                rep = self._evaluate(model, val)
                rep["calibration_points"] = len(data)
                rep["fit_lambda"] = model.lam
                key = self._result(s, rep)
                if key == 27:
                    return None
                if key == 13:
                    # 驗證資料也加入最終模型，讓樣本更多
                    Xv = [np.array([f.vec for f in fs]) for _, fs in val if fs]
                    if Xv:
                        X = np.vstack([X] + Xv)
                        Y = np.vstack([Y] + [np.tile(t, (len(fs), 1)) for t, fs in val if fs])
                        G = np.concatenate([G] + [np.full(len(fs), 100 + i) for i, (_, fs) in enumerate(val) if fs])
                        model = GazeModel().fit(X, Y, G)
                    return Calibration(X, Y, G, model, rep)
        finally:
            s.close()


def live_test(tracker: FaceTracker, calib: Calibration, mon: win32.Monitor, seconds: Optional[float] = None):
    """全螢幕顯示即時視線點與參考格線，用來目測準確度。Esc 結束。"""
    import cv2
    s = Screen(mon)
    filt = OneEuro()
    blink = BlinkFilter()
    last_t = None
    trail: list[tuple[int, int]] = []
    t0 = time.time()
    try:
        while seconds is None or time.time() - t0 < seconds:
            img = s.blank()
            for gx in (0.1, 0.3, 0.5, 0.7, 0.9):
                for gy in (0.1, 0.5, 0.9):
                    cv2.circle(img, s.px((gx, gy)), int(6 * s.scale), MUTED, 1, cv2.LINE_AA)
            f = tracker.latest()
            if (f is not None and f.vec is not None and not blink(f.t_ms, f.openness)
                    and f.openness >= 0.5 * calib.model.open_ref and f.t_ms != last_t):
                last_t = f.t_ms
                raw = calib.model.predict(f.vec)[0]
                ok = (calib.model.head_z(f.vec) <= WebcamGazeSource.HEAD_Z_MAX
                      and -0.5 <= raw[0] <= 1.5 and -0.5 <= raw[1] <= 1.5)
                if ok:
                    p = filt(raw, f.t_ms / 1000)
                    trail.append(s.px(p))
                    trail = trail[-20:]
            for i in range(1, len(trail)):
                cv2.line(img, trail[i - 1], trail[i], ACCENT, max(1, int(i / 4)), cv2.LINE_AA)
            if trail:
                cv2.circle(img, trail[-1], int(22 * s.scale), ACCENT, 3, cv2.LINE_AA)
            s.text(img, f"即時測試：看看格線上的圓圈，紅圈應跟著你的視線　Esc 結束　（{tracker.fps:.0f} fps）",
                   y=int(s.H * 0.02), size=24, color=MUTED)
            if s.show(img, 1) == 27:
                break
    finally:
        s.close()
