"""「造訪段落」：使用者連續停留在同一分頁、同一頁面的一段時間。

換網址（不含 #fragment）或切換分頁就開始新段落；之後再回到同一網址也是新段落。
整頁視線圖以段落為單位顯示，樣本與注視點都標上段落編號。
"""
from __future__ import annotations

from typing import Optional

MIN_VISIT_MS = 300  # 比這短、又沒有任何視線樣本的段落視為切換時的閃動，丟掉


def page_key(url: Optional[str]) -> Optional[str]:
    """整頁視線圖用的頁面識別：去掉 #fragment（單頁應用常只改 hash，畫面仍是同一頁）。"""
    return url.split("#", 1)[0] if url else url


def derive_visits(samples: list[dict]) -> list[dict]:
    """舊錄製沒有段落紀錄時，從樣本的 (分頁, 網址) 連續區段推回段落。"""
    out: list[dict] = []
    for s in samples:
        if not s.get("url"):
            continue
        key = page_key(s["url"])
        cur = out[-1] if out else None
        if cur and cur["key"] == key and cur["tab"] == s.get("tab"):
            cur["end_ms"] = s["t_ms"]
        else:
            out.append({"tab": s.get("tab"), "url": s["url"], "key": key, "title": "",
                        "start_ms": s["t_ms"], "end_ms": s["t_ms"]})
    return out


def build_visits(meta: dict, samples: list[dict]) -> list[dict]:
    raw = meta.get("visits") or derive_visits(samples)
    # about:blank 等非網頁（新分頁開啟時的空白頁）不算段落
    visits = [dict(v) for v in raw if str(v.get("url", "")).startswith(("http", "file"))]
    titles = {u: (info or {}).get("title") for u, info in (meta.get("urls") or {}).items()}
    for v in visits:
        v.setdefault("key", page_key(v.get("url")))
        if not v.get("title"):
            v["title"] = titles.get(v["url"]) or ""
    assign_samples(visits, samples, write=False)
    counts: dict[int, int] = {}
    for s in samples:
        i = s.get("_visit")
        if i is not None:
            counts[i] = counts.get(i, 0) + 1
    keep = [v for i, v in enumerate(visits)
            if counts.get(i, 0) > 0 or v["end_ms"] - v["start_ms"] >= MIN_VISIT_MS]
    for i, v in enumerate(keep):
        v["index"] = i + 1
    for s in samples:
        s.pop("_visit", None)
    return keep


def find_visit(visits: list[dict], t_ms: float, key: Optional[str], tab=None, slack_ms: float = 1000) -> Optional[int]:
    """回傳段落在 visits 中的索引：同網址（與分頁）且時間落在段落內（容許一點誤差）。"""
    best = None
    for i, v in enumerate(visits):
        if v["key"] != key or (tab is not None and v.get("tab") is not None and v["tab"] != tab):
            continue
        if v["start_ms"] - slack_ms <= t_ms <= v["end_ms"] + slack_ms:
            # 時間重疊時取「開始時間最接近且不晚於 t」的段落
            if best is None or (v["start_ms"] <= t_ms and v["start_ms"] > visits[best]["start_ms"]):
                best = i
    return best


def assign_samples(visits: list[dict], samples: list[dict], write: bool = True):
    """把每個樣本對應到段落。write=True 時寫入 sample['visit']（1 起算的段落編號）。"""
    for s in samples:
        i = find_visit(visits, s["t_ms"], page_key(s.get("url")), s.get("tab")) if s.get("url") else None
        if write:
            s["visit"] = visits[i]["index"] if i is not None else None
        else:
            s["_visit"] = i
