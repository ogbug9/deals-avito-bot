"""Аналоги: похожие лоты той же позиции и модели близкой производительности (words/analogs.json).

Индексы производительности в файле — ориентировочные, их правит пользователь. Программа их не «знает»:
нет записи — нет индекса и нет цены за производительность.
"""
from __future__ import annotations

import json
import statistics

from . import db
from .paths import home, package_dir

FILE = "analogs.json"


def path():
    user = home() / "words" / FILE
    return user if user.exists() else package_dir() / "words" / FILE


def load() -> dict:
    """{'approx': bool, 'groups': [{'name', 'note', 'models': {ключ: индекс|None}}]} — битый файл даёт пустую таблицу."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        groups = [{"name": str(g.get("name", "")), "note": str(g.get("note", "")),
                   "models": {str(k): (float(v) if isinstance(v, (int, float)) else None) for k, v in g.get("models", {}).items()}}
                  for g in data.get("groups", []) if isinstance(g, dict)]
        return {"approx": bool(data.get("approx", True)), "baseline": str(data.get("baseline", "")), "groups": groups,
                "error": ""}
    except (OSError, ValueError, AttributeError) as e:
        return {"approx": True, "baseline": "", "groups": [], "error": f"не удалось прочитать {FILE}: {e}"}


def group_of(key: str, table: dict | None = None) -> dict | None:
    for g in (table or load())["groups"]:
        if key in g["models"]:
            return g
    return None


def index_of(key: str, table: dict | None = None) -> float | None:
    g = group_of(key, table)
    return g["models"][key] if g else None


def cheaper_equivalents(conn, key: str, now: float, min_n: int = 6, table: dict | None = None) -> dict:
    """Модели из той же группы, чья медиана ниже: сколько экономии и какая разница в индексе."""
    table = table or load()
    g = group_of(key, table)
    if not g:
        return {"group": None, "approx": table["approx"], "items": [], "own": None}
    own = db.market_stats(conn, key, None, now)
    items = []
    for other, idx in g["models"].items():
        if other == key:
            continue
        ms = db.market_stats(conn, other, None, now)
        if ms.n < min_n or (own.n >= min_n and ms.median_ask >= own.median_ask):
            continue
        mine = g["models"][key]
        items.append({"key": other, "median": ms.median_ask, "n": ms.n, "index": idx,
                      "saving": own.median_ask - ms.median_ask if own.n >= min_n else None,
                      "index_diff": (idx - mine) / mine if idx and mine else None})
    items.sort(key=lambda x: x["median"])
    return {"group": g["name"], "note": g["note"], "approx": table["approx"], "items": items,
            "own": {"median": own.median_ask, "n": own.n, "index": g["models"][key]} if own.n else None}


def similar(conn, lid: str, now: float, limit: int = 8) -> dict:
    """Похожие лоты: та же позиция, ближайшие по цене; у каждого метка «дешевле / дороже рынка».
    Плюс все цены позиции — для диаграммы разброса с отметкой этого лота."""
    r = conn.execute("SELECT item_key, price FROM listings WHERE id=?", (lid,)).fetchone()
    if not r or not r["item_key"]:
        return {"key": None, "items": [], "prices": [], "median": None, "n": 0}
    rows = conn.execute(
        "SELECT id, title, price, url, city, delivery, image, date_text, est_profit, mode_tags FROM listings "
        "WHERE item_key=? AND status='active' AND defect=0 AND wanted=0 AND price>0 AND id<>?",
        (r["item_key"], lid)).fetchall()
    prices = sorted(x["price"] for x in rows)
    all_prices = sorted(prices + ([r["price"]] if r["price"] else []))
    median = statistics.median(all_prices) if all_prices else None
    near = sorted(rows, key=lambda x: abs(x["price"] - (r["price"] or 0)))[:limit]
    items = []
    for x in sorted(near, key=lambda x: x["price"]):
        ratio = x["price"] / median - 1 if median else 0.0
        items.append({**dict(x), "vs_market": ratio,
                      "label": "дешевле рынка" if ratio <= -0.05 else "дороже рынка" if ratio >= 0.05 else "по рынку",
                      "vs_this": x["price"] - (r["price"] or 0)})
    below = sum(1 for p in all_prices if p < (r["price"] or 0))
    return {"key": r["item_key"], "items": items, "prices": all_prices, "median": median, "n": len(all_prices),
            "price": r["price"], "cheaper_than_share": 1 - below / len(all_prices) if all_prices else None}


def ceilings(conn, now: float, min_n: int = 6, table: dict | None = None) -> dict[str, tuple[float, str]]:
    """Для каждой модели с индексом — самая дешёвая медиана среди более сильных моделей, если она ниже своей.

    {модель: (цена, какая модель)}. Смысл: слабую карту не продать быстро дороже, чем просят за сильную.
    Индексы берутся из words/analogs.json; моделей без индекса правило не касается.
    """
    table = table or load()
    index = {k: v for g in table["groups"] for k, v in g["models"].items() if v}
    medians: dict[str, float] = {}
    for key in index:
        ms = db.market_stats(conn, key, None, now)
        if ms.n >= min_n:
            medians[key] = ms.median_ask
    out: dict[str, tuple[float, str]] = {}
    for key, med in medians.items():
        stronger = [(m, k) for k, m in medians.items() if k != key and index[k] >= index[key] * 1.05]
        if stronger:
            cap, cap_key = min(stronger)
            if cap < med:
                out[key] = (cap, cap_key)
    return out
