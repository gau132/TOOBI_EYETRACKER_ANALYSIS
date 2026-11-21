"""把一個錄製資料夾打包成可離線開啟的 replay.html（rrweb 與資料都內嵌）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent


def _safe_json(obj) -> str:
    # 內嵌在 <script> 裡，避免出現 </script>
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _is_attach(e: dict) -> bool:
    d = e.get("data") or {}
    return e.get("type") == 3 and d.get("source") == 0 and bool(d.get("isAttachIframe")) and bool(d.get("adds"))


def reattach_iframes(events: list[dict], all_events: Optional[list[dict]] = None) -> list[dict]:
    """每個完整快照之後，重新附上該分頁各 iframe 截至當時最近一次的內容。

    跨網域 iframe 的內容由子框架另外送來（rrweb isAttachIframe 事件），父頁面的完整快照不會包含它；
    iframe 比父頁面早送達、或切換分頁時補上的快照之後，回放就會變成空白框。
    """
    attaches = sorted((e for e in (all_events or events) if _is_attach(e)), key=lambda e: e["timestamp"])
    out: list[dict] = []
    for e in events:
        out.append(e)
        if e.get("type") == 2:
            last: dict = {}
            for a in attaches:
                if a["timestamp"] > e.get("_orig_ts", e["timestamp"]):
                    break
                if a.get("tab") == e.get("tab"):
                    last[a["data"]["adds"][0].get("parentId")] = a
            out.extend({**a, "timestamp": e["timestamp"]} for a in last.values())
    return out


def select_active_events(events: list[dict], visits: list[dict]) -> list[dict]:
    """所有分頁都會錄製；依造訪段落只保留「當時在最前面的分頁」的事件。

    切到某分頁時，先補上該分頁在切換前最後一次完整快照與之後的變化（時間改成切換當下），
    回放才能立刻重建那個分頁當時的畫面。
    """
    tabs_with_events = {e.get("tab") for e in events}
    segs = [(v["start_ms"], v.get("tab")) for v in sorted(visits, key=lambda v: v["start_ms"])
            if v.get("tab") in tabs_with_events]
    if not segs:
        return events
    # 同一分頁連續的段落（在同分頁內換頁）合併成一段「分頁期間」
    runs: list[list] = []
    for start, tab in segs:
        if not runs or runs[-1][1] != tab:
            runs.append([start, tab])
    by_tab: dict = {}
    for e in events:
        by_tab.setdefault(e.get("tab"), []).append(e)
    out: list[dict] = []
    for i, (start, tab) in enumerate(runs):
        lo = float("-inf") if i == 0 else start
        end = runs[i + 1][0] if i + 1 < len(runs) else float("inf")
        tev = by_tab.get(tab, [])
        if lo != float("-inf"):
            fs = [k for k, e in enumerate(tev) if e["type"] == 2 and e["timestamp"] <= lo]
            if fs:
                k0 = fs[-1] - 1 if fs[-1] > 0 and tev[fs[-1] - 1]["type"] == 4 else fs[-1]
                for e in tev[k0:]:
                    if e["timestamp"] > lo:
                        break
                    out.append({**e, "timestamp": lo, "_orig_ts": e["timestamp"]})
        out.extend(e for e in tev if lo < e["timestamp"] < end)
    return out


def _visits_for_page_view(meta: dict) -> list[dict]:
    """每個段落配上背景：優先用該段落期間拍的可視區截圖；沒有就借同網址其他時間的截圖；再沒有用補拍的整頁圖。"""
    from .visits import find_visit

    visits = [dict(v) for v in meta.get("visits", [])]
    for v in visits:
        v["tiles"], v["bg"] = [], "none"
    for key, tiles in (meta.get("page_tiles") or {}).items():
        for t in tiles:
            i = find_visit(visits, t["t"], key, slack_ms=300)
            if i is not None:
                visits[i]["tiles"].append(t)
    shots = meta.get("page_shots") or {}
    for v in visits:
        if v["key"] in shots:
            v["shot"] = shots[v["key"]]  # 背景完整長截圖（一次截完整頁，捲動太快也不缺）
        if not v["tiles"]:
            borrowed = [t for o in visits if o is not v and o["key"] == v["key"] for t in o["tiles"]]
            if borrowed:
                v["tiles"], v["tiles_borrowed"] = sorted(borrowed, key=lambda t: t["t"]), True
        v["bg"] = "shot" if v.get("shot") else ("tiles" if v["tiles"] else "none")
    return visits


def _scroll_lookup(samples: list[dict]):
    """每個段落的（時間 → 捲動量）查表，用來把滑鼠的視窗座標換成整頁座標。"""
    import bisect
    per: dict = {}
    for s in samples:
        if s.get("visit") and s.get("scroll_y") is not None:
            per.setdefault(s["visit"], []).append((s["t_ms"], s.get("scroll_x") or 0, s["scroll_y"]))
    keys = {v: [p[0] for p in pts] for v, pts in per.items()}

    def at(visit, t):
        pts = per.get(visit)
        if not pts:
            return 0.0, 0.0
        i = max(0, min(len(pts) - 1, bisect.bisect_right(keys[visit], t) - 1))
        return pts[i][1], pts[i][2]
    return at


def mouse_and_clicks(events: list[dict], samples: list[dict], visits: list[dict]):
    """從 rrweb 事件取出滑鼠移動與點擊（視窗座標），配上當時的捲動量得到整頁座標。

    回傳 (mouse, clicks)，每筆 [t_ms, viewport_x, viewport_y, page_x, page_y, visit]。
    只取當時作用中分頁的事件；跨網域 iframe 內的滑鼠座標相對於 iframe，可能有偏移。
    """
    scroll_at = _scroll_lookup(samples)

    def visit_of(t, tab):
        for v in visits:
            if v.get("tab") == tab and v["start_ms"] - 300 <= t <= v["end_ms"] + 300:
                return v["index"]
        return None

    mouse, clicks, last = [], [], {}
    for e in events:
        d = e.get("data") or {}
        if e.get("type") != 3:
            continue
        if d.get("source") in (1, 6):  # MouseMove / TouchMove
            for p in d.get("positions", []):
                t = e["timestamp"] + p.get("timeOffset", 0)
                vi = visit_of(t, e.get("tab"))
                if vi is None or t - last.get(vi, -1e9) < 100:  # 最多 10 Hz
                    continue
                last[vi] = t
                sx, sy = scroll_at(vi, t)
                mouse.append([round(t, 1), p["x"], p["y"], round(p["x"] + sx, 1), round(p["y"] + sy, 1), vi])
        elif d.get("source") == 2 and d.get("type") == 2 and d.get("x") is not None:  # Click
            t = e["timestamp"]
            vi = visit_of(t, e.get("tab"))
            if vi is None:
                continue
            sx, sy = scroll_at(vi, t)
            clicks.append([round(t, 1), d["x"], d["y"], round(d["x"] + sx, 1), round(d["y"] + sy, 1), vi])
    mouse.sort(key=lambda r: r[0])
    clicks.sort(key=lambda r: r[0])
    return mouse, clicks


def timeline_track(samples: list[dict], meta: dict, step_ms: float = 250.0):
    """時間軸用：每 0.25 秒一格的 [水平視線 0–1, 捲動 y]，沒有資料的格子為 null。"""
    t0, t1 = meta["started_ms"], meta["ended_ms"]
    n = max(1, int((t1 - t0) / step_ms) + 1)
    gx = [[] for _ in range(n)]
    sc = [None] * n
    for s in samples:
        i = int((s["t_ms"] - t0) / step_ms)
        if 0 <= i < n:
            if s.get("valid") and s.get("norm_x") is not None:
                gx[i].append(s["norm_x"])
            if s.get("scroll_y") is not None:
                sc[i] = s["scroll_y"]
    return {"step_ms": step_ms,
            "x": [round(sorted(v)[len(v) // 2], 3) if v else None for v in gx],
            "scroll": sc}


def build_replay(session_dir: Path) -> Path:
    from .export import FIX_COLUMNS, GAZE_COLUMNS, VISIT_COLUMNS, detect_fixations

    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    samples = json.loads((session_dir / "gaze.json").read_text(encoding="utf-8"))
    events = json.loads((session_dir / "events.json").read_text(encoding="utf-8"))
    fixes = detect_fixations(samples)

    urls: list[str] = []
    uidx: dict[str, int] = {}

    def u(x):
        if x is None:
            return -1
        x = x.split("#", 1)[0]  # 整頁視線圖以去掉 #fragment 的網址分組
        if x not in uidx:
            uidx[x] = len(urls)
            urls.append(x)
        return uidx[x]

    # 壓縮成陣列：[t_ms, viewport_x, viewport_y, page_x, page_y, in_viewport, tab, url_index, visit]
    compact = [
        [s["t_ms"], s["viewport_x"], s["viewport_y"], s["page_x"], s["page_y"], s["in_viewport"], s["tab"],
         u(s["url"]), s.get("visit") or 0]
        for s in samples if s.get("valid") and s.get("page_x") is not None
    ]
    fx = [[f["start_ms"], f["duration_ms"], f["viewport_x"], f["viewport_y"], f["page_x"], f["page_y"], u(f["url"]),
           f.get("visit") or 0]
          for f in fixes if f.get("page_x") is not None]
    shown = reattach_iframes(select_active_events(events, meta.get("visits") or []), events)
    for e in shown:
        e.pop("_orig_ts", None)
    mouse, clicks = mouse_and_clicks(events, samples, meta.get("visits") or [])

    # 匯出用的完整表格（欄位同 CSV；網址以索引存放以節省大小）
    eurls: list[str] = []
    eidx: dict[str, int] = {}

    def eu(x):
        if x is None:
            return None
        if x not in eidx:
            eidx[x] = len(eurls)
            eurls.append(x)
        return eidx[x]

    def rows(objs, cols):
        return [[eu(o.get(c)) if c == "url" else o.get(c) for c in cols] for o in objs]

    export = {
        "urls": eurls,
        "gaze": {"columns": GAZE_COLUMNS, "rows": rows(samples, GAZE_COLUMNS)},
        "fixations": {"columns": FIX_COLUMNS, "rows": rows(fixes, FIX_COLUMNS)},
        "visits": {"columns": VISIT_COLUMNS, "rows": rows(meta.get("visits") or [], VISIT_COLUMNS)},
    }
    data = {"meta": meta, "urls": urls, "gaze": compact, "fixations": fx, "events": shown,
            "visits": _visits_for_page_view(meta), "mouse": mouse, "clicks": clicks,
            "track": timeline_track(samples, meta), "export": export}

    html = (HERE / "replay_template.html").read_text(encoding="utf-8")
    html = (html.replace("/*__RRWEB_CSS__*/", (HERE / "vendor" / "rrweb.min.css").read_text(encoding="utf-8"))
                .replace("/*__RRWEB_JS__*/", (HERE / "vendor" / "rrweb.min.js").read_text(encoding="utf-8").replace("</script", "<\\/script"))
                .replace("__DATA_JSON__", _safe_json(data)))
    out = session_dir / "replay.html"
    out.write_text(html, encoding="utf-8")
    return out
