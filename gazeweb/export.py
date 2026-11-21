"""存檔與匯出：視線樣本 CSV/JSON、注視點 (fixation) 偵測、回放 HTML。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

GAZE_COLUMNS = [
    "t_ms", "t_rel_ms", "device_ts_us", "valid", "norm_x", "norm_y", "screen_x", "screen_y",
    "viewport_x", "viewport_y", "page_x", "page_y", "scroll_x", "scroll_y", "in_viewport",
    "url", "tab", "visit", "mapping",
]
FIX_COLUMNS = [
    "index", "start_ms", "end_ms", "start_rel_ms", "duration_ms", "samples",
    "screen_x", "screen_y", "viewport_x", "viewport_y", "page_x", "page_y", "url", "tab", "visit",
]
VISIT_COLUMNS = ["visit", "start_ms", "end_ms", "start_rel_ms", "duration_ms", "tab", "url", "title",
                 "samples", "fixations"]


def _write_csv(path: Path, cols: list[str], rows: list[dict]):
    # utf-8-sig：讓 Excel 直接開啟中文網址不亂碼
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})


def detect_fixations(samples: list[dict], dispersion_px: float = 50.0, min_ms: float = 100.0) -> list[dict]:
    """I-DT（離散度閾值）注視點偵測，在螢幕實體像素上計算。

    dispersion_px：(max_x-min_x)+(max_y-min_y) 的上限，約 1° 視角（60 cm 距離、一般螢幕）。
    """
    pts = [s for s in samples if s.get("valid") and s.get("screen_x") is not None]
    fixes: list[dict] = []
    i, n = 0, len(pts)

    def disp(win):
        xs = [p["screen_x"] for p in win]
        ys = [p["screen_y"] for p in win]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))

    while i < n:
        j = i
        while j < n and pts[j]["t_ms"] - pts[i]["t_ms"] < min_ms:
            j += 1
        if j >= n:
            break
        if disp(pts[i:j + 1]) <= dispersion_px:
            while j + 1 < n and disp(pts[i:j + 2]) <= dispersion_px and pts[j + 1]["t_ms"] - pts[j]["t_ms"] < 100:
                j += 1
            win = pts[i:j + 1]
            mapped = [p for p in win if p.get("page_x") is not None]

            def avg(key, src):
                return round(sum(p[key] for p in src) / len(src), 1) if src else None

            fixes.append({
                "index": len(fixes) + 1,
                "start_ms": win[0]["t_ms"], "end_ms": win[-1]["t_ms"],
                "start_rel_ms": win[0]["t_rel_ms"],
                "duration_ms": round(win[-1]["t_ms"] - win[0]["t_ms"], 1),
                "samples": len(win),
                "screen_x": avg("screen_x", win), "screen_y": avg("screen_y", win),
                "viewport_x": avg("viewport_x", mapped), "viewport_y": avg("viewport_y", mapped),
                "page_x": avg("page_x", mapped), "page_y": avg("page_y", mapped),
                "url": mapped[len(mapped) // 2]["url"] if mapped else None,
                "tab": mapped[len(mapped) // 2]["tab"] if mapped else None,
                "visit": mapped[len(mapped) // 2].get("visit") if mapped else None,
            })
            i = j + 1
        else:
            i += 1
    return fixes


def fix_inner_scroll(meta: dict, samples: list[dict], events: list[dict]) -> int:
    """舊錄製的修正：網站捲動的是頁面內的容器（window.scrollY 一直是 0）時，
    用 rrweb 記錄的元素捲動事件，把當下的捲動量補進整頁座標與截圖位置。回傳修正的樣本數。"""
    import bisect

    from .visits import find_visit, page_key

    if meta.get("scroll_model"):
        return 0
    roots = {e.get("tab"): e["data"]["node"].get("id") for e in events if e.get("type") == 2}
    per_tab: dict = {}
    for e in events:
        d = e.get("data") or {}
        if e.get("type") == 3 and d.get("source") == 3 and d.get("id") != roots.get(e.get("tab")):
            per_tab.setdefault(e.get("tab"), {}).setdefault(d["id"], []).append((e["timestamp"], d.get("x", 0), d.get("y", 0)))
    track: dict = {}
    for tab, by_id in per_tab.items():
        node, sc = max(by_id.items(), key=lambda kv: len(kv[1]))
        if len(sc) >= 3 and max(abs(y) for _, _, y in sc) > 50:  # 有明顯捲動的主要容器
            sc.sort()
            track[tab] = ([t for t, _, _ in sc], sc)
    if not track:
        meta["scroll_model"] = "window"
        return 0

    def offset(tab, t):
        ts, sc = track[tab]
        i = bisect.bisect_right(ts, t) - 1
        return (sc[i][1], sc[i][2]) if i >= 0 else (0, 0)

    fixed = 0
    for s in samples:
        if s.get("page_x") is None or s.get("tab") not in track or s.get("scroll_y"):
            continue
        ox, oy = offset(s["tab"], s["t_ms"])
        s["page_x"], s["page_y"] = round(s["page_x"] + ox, 1), round(s["page_y"] + oy, 1)
        s["scroll_x"], s["scroll_y"] = ox, oy
        fixed += 1
    visits = meta.get("visits") or []
    for key, tiles in (meta.get("page_tiles") or {}).items():
        for t in tiles:
            i = find_visit(visits, t["t"], page_key(key), slack_ms=500) if visits else None
            tab = visits[i].get("tab") if i is not None else next(iter(track))
            if tab in track and not t.get("sy"):
                ox, oy = offset(tab, t["t"])
                t["sx"], t["sy"] = ox, oy
    meta["scroll_model"] = "inner-scroll-fixed"
    return fixed


def write_session(out_dir: Path, meta: dict, samples: list[dict], events: list[dict]):
    from .visits import assign_samples, build_visits

    out_dir.mkdir(parents=True, exist_ok=True)
    meta = dict(meta)
    n_fixed = fix_inner_scroll(meta, samples, events)
    if n_fixed:
        print(f"  偵測到頁面內捲動容器：已用 rrweb 捲動事件修正 {n_fixed} 個樣本的整頁座標")
    visits = build_visits(meta, samples)
    assign_samples(visits, samples)
    fixes = detect_fixations(samples)
    t0 = meta.get("started_ms", 0)
    for v in visits:
        v["start_rel_ms"] = round(v["start_ms"] - t0, 1)
        v["duration_ms"] = round(v["end_ms"] - v["start_ms"], 1)
        v["samples"] = sum(1 for s in samples if s.get("visit") == v["index"])
        v["fixations"] = sum(1 for f in fixes if f.get("visit") == v["index"])
        v["visit"] = v["index"]
    meta["visits"] = visits
    meta["counts"] = {"samples": len(samples), "valid_samples": sum(s["valid"] for s in samples),
                      "mapped_samples": sum(1 for s in samples if s.get("page_x") is not None),
                      "fixations": len(fixes), "rrweb_events": len(events), "visits": len(visits)}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "gaze.json").write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
    (out_dir / "events.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "gaze.csv", GAZE_COLUMNS, samples)
    _write_csv(out_dir / "fixations.csv", FIX_COLUMNS, fixes)
    _write_csv(out_dir / "visits.csv", VISIT_COLUMNS, visits)
    from .replay import build_replay
    build_replay(out_dir)
    c = meta["counts"]
    print(f"已存檔：{out_dir}\n  樣本 {c['samples']}（有效 {c['valid_samples']}、對應到網頁 {c['mapped_samples']}）"
          f"，注視點 {c['fixations']}，rrweb 事件 {c['rrweb_events']}")


def rebuild(out_dir: Path):
    """重新從 gaze.json / events.json 產生 CSV、注視點與回放頁（例如調整演算法後）。"""
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    samples = json.loads((out_dir / "gaze.json").read_text(encoding="utf-8"))
    events = json.loads((out_dir / "events.json").read_text(encoding="utf-8"))
    meta.pop("counts", None)
    write_session(out_dir, meta, samples, events)
