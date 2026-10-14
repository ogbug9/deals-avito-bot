"""Сравнение лотов (колонки — лоты, строки — критерии) и сравнение моделей.

Для каждого критерия известно направление: что лучше — больше или меньше. Лучшее значение в строке помечается
best, худшее — worst; если значения совпадают, меток нет. К эталонной колонке считается разница.
"""
from __future__ import annotations

import csv
import io
import json
import statistics
import time

from . import analogs, db, modes

MAX_LOTS = 6
FLAG_RU = {"mining": "майнинг", "untested": "не тестировалась", "prepay": "предоплата", "repaired": "был ремонт",
           "flashed": "прошивка", "no_testing_allowed": "без проверки", "issues": "есть нюансы",
           "no_warranty": "без гарантии", "model_from_desc": "модель не из заголовка", "vram_mismatch": "память не как у модели", "obfuscated": "текст замаскирован", "no_outputs": "нет видеовыходов", "reseller": "перекупщик / дубли"}
SOURCE_RU = {"title": "из заголовка", "description": "из описания", "fuzzy": "из опечатки"}


def _rub(v) -> str:
    return "—" if v is None else f"{v:,.0f} ₽".replace(",", " ")


def _pct(v, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.0f}%" if signed else f"{v * 100:.0f}%"


def _days(v) -> str:
    return "—" if v is None else ("меньше дня" if v < 1 else f"{v:.0f} дн.")


# (ключ, подпись, направление: +1 — больше лучше, -1 — меньше лучше, 0 — не сравнивается, формат значения)
CRITERIA = [
    ("photo", "Фото", 0, None),
    ("price", "Цена", -1, _rub),
    ("vs_median", "К медиане позиции", -1, lambda v: _pct(v, True)),
    ("est_profit", "Расчётная прибыль", +1, _rub),
    ("net", "Чистыми после торга", +1, _rub),
    ("ask_net", "Чистыми без торга", +1, _rub),
    ("model", "Модель и объём", 0, None),
    ("condition", "Состояние", 0, None),
    ("city", "Город / метро", 0, None),
    ("delivery", "Авито Доставка", +1, lambda v: "есть" if v else "нет"),
    ("age", "Возраст объявления", 0, _days),
    ("listed", "Висит у нас в базе", 0, _days),
    ("drop", "Снижение цены", +1, lambda v: _rub(v) if v else "не снижалась"),
    ("seller", "Продавец", 0, None),
    ("rating", "Рейтинг продавца", +1, lambda v: "—" if v is None else f"{v:.1f}".replace(".", ",")),
    ("seller_ads", "Объявлений продавца в базе", -1, lambda v: "—" if v is None else f"{v:.0f}"),
    ("modes", "Режимы", 0, None),
    ("risk", "Штраф за риск", -1, lambda v: "нет" if not v else f"{v * 100:.0f}%"),
    ("risk_words", "Признаки риска и стоп-слова", 0, None),
    ("n", "Доверие: объявлений в позиции", +1, lambda v: "—" if v is None else f"n={v:.0f}"),
    ("source", "Доверие: откуда модель", 0, None),
    ("liquidity", "Срок продажи позиции", -1, _days),
]


def _lot(conn, r, now: float) -> dict:
    sc = json.loads(r["score_json"]) if r["score_json"] else {}
    flags = json.loads(r["flags"] or "{}")
    median = sc.get("median_ask") if sc.get("market_n", 0) >= 6 else None
    liq = db.liquidity(conn, r["item_key"], now) if r["item_key"] else {"days": None, "rough": True, "n": 0}
    start = r["posted_at"] or r["first_seen"]
    risk_words = [FLAG_RU.get(k, k) for k in flags] + (["БРАК (стоп-слово)"] if r["defect"] else [])
    seller = " · ".join(x for x in (r["seller_name"], r["seller_type"]) if x)
    return {
        "photo": r["image"] or "",
        "price": r["price"] or None,
        "vs_median": (r["price"] / median - 1) if median and r["price"] else None,
        "est_profit": r["est_profit"],
        "net": sc.get("net") if sc.get("target_buy") else None,
        "ask_net": sc.get("ask_net") if sc.get("target_buy") else None,
        "model": r["item_key"] or "не определена",
        "condition": r["avito_condition"] or "не проверено",
        "city": r["city"] or "—",
        "delivery": int(bool(r["delivery"])),
        "age": (now - r["posted_at"]) / 86400 if r["posted_at"] else None,
        "listed": (now - r["first_seen"]) / 86400 if r["first_seen"] else None,
        "drop": (r["first_price"] - r["price"]) if r["first_price"] and r["price"] and r["first_price"] > r["price"] else 0,
        "seller": seller or "—",
        "rating": r["rating"],
        "seller_ads": r["seller_ads"],
        "modes": ", ".join(modes.MODE_TITLES.get(m, m) for m in (r["mode_tags"] or "").split(",") if m) or "—",
        "risk": min(0.6, sum(flags.values())) if flags else 0.0,
        "risk_words": ", ".join(risk_words) or "нет",
        "n": sc.get("market_n") or None,
        "source": SOURCE_RU.get(r["model_source"] or "", "—") if r["item_key"] else "—",
        "liquidity": liq["days"],
        "_liq_rough": liq["rough"] and liq["days"] is not None,
        "_start": start,
    }


def _mark(values: list, direction: int) -> list[str]:
    """Метки best / worst для строки. Пустые значения не участвуют; при одинаковых значениях меток нет."""
    marks = [""] * len(values)
    nums = [(i, v) for i, v in enumerate(values) if isinstance(v, (int, float)) and v is not None]
    if not direction or len({v for _, v in nums}) < 2:
        return marks
    best = max(v for _, v in nums) if direction > 0 else min(v for _, v in nums)
    worst = min(v for _, v in nums) if direction > 0 else max(v for _, v in nums)
    for i, v in nums:
        marks[i] = "best" if v == best else "worst" if v == worst else ""
    return marks


def _diff(key: str, value, ref) -> str:
    if not isinstance(value, (int, float)) or not isinstance(ref, (int, float)) or value == ref:
        return ""
    d = value - ref
    if key in ("vs_median", "risk"):
        return f"{d * 100:+.0f} п.п."
    if key in ("age", "listed", "liquidity"):
        return f"{d:+.0f} дн."
    if key in ("rating",):
        return f"{d:+.1f}".replace(".", ",")
    if key in ("n", "seller_ads", "delivery"):
        return f"{d:+.0f}"
    return f"{d:+,.0f} ₽".replace(",", " ")


def build(conn, ids: list[str], ref: str | None = None, now: float | None = None) -> dict:
    """Таблица сравнения. ids — до MAX_LOTS объявлений в порядке колонок; ref — id эталонной колонки."""
    now = time.time() if now is None else now
    ids = list(dict.fromkeys(str(i) for i in ids if i))[:MAX_LOTS]
    found = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM listings WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    ids = [i for i in ids if i in found]
    lots = [_lot(conn, found[i], now) for i in ids]
    ref_i = ids.index(ref) if ref in ids else None
    rows = []
    for key, label, direction, fmt in CRITERIA:
        raw = [lot[key] for lot in lots]
        marks = _mark(raw, direction)
        texts = [(fmt(v) if fmt else (v if v not in (None, "") else "—")) for v in raw]
        if key == "liquidity":
            texts = [t + (" (оценка грубая)" if lot["_liq_rough"] else "") for t, lot in zip(texts, lots)]
        cells = [{"text": t, "raw": v, "mark": m, "diff": _diff(key, v, raw[ref_i]) if ref_i is not None and i != ref_i else ""}
                 for i, (t, v, m) in enumerate(zip(texts, raw, marks))]
        rows.append({"key": key, "label": label, "direction": direction, "cells": cells,
                     "differs": len({c["text"] for c in cells}) > 1})
    return {"lots": [{"id": i, "title": found[i]["title"], "url": found[i]["url"], "image": found[i]["image"],
                      "status": found[i]["status"]} for i in ids],
            "ref": ids[ref_i] if ref_i is not None else None, "rows": rows}


def only_differences(result: dict) -> list[dict]:
    return [r for r in result["rows"] if r["differs"]]


def to_csv(result: dict, only_diff: bool = False) -> str:
    """CSV для Excel: разделитель «;», первая колонка — критерий."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Критерий"] + [lot["title"] for lot in result["lots"]])
    w.writerow(["Ссылка"] + [lot["url"] or "" for lot in result["lots"]])
    for r in (only_differences(result) if only_diff else result["rows"]):
        if r["key"] == "photo":
            continue
        w.writerow([r["label"]] + [c["text"] + (f" ({c['diff']})" if c["diff"] else "") for c in r["cells"]])
    return "﻿" + buf.getvalue()


# ---------------------------------------------------------------- сравнение моделей
MODEL_CRITERIA = [
    ("n", "Объявлений сейчас", +1, lambda v: "—" if v is None else f"{v:.0f}"),
    ("median", "Медиана", -1, _rub),
    ("min", "Минимальная цена", -1, _rub),
    ("change7", "Изменение медианы за 7 дней", 0, lambda v: _pct(v, True)),
    ("change30", "Изменение медианы за 30 дней", 0, lambda v: _pct(v, True)),
    ("new7", "Новых за 7 дней", +1, lambda v: "—" if v is None else f"{v:.0f}"),
    ("gone7", "Ушло за 7 дней", +1, lambda v: "—" if v is None else f"{v:.0f}"),
    ("liquidity", "Срок продажи", -1, _days),
    ("index", "Индекс производительности (ориентировочно)", +1, lambda v: "нет данных" if v is None else f"{v:.0f}"),
    ("per_index", "Цена за пункт индекса", -1, lambda v: "—" if v is None else _rub(v)),
]


def _change(conn, key: str, days: int, now: float) -> float | None:
    since = time.strftime("%Y-%m-%d", time.localtime(now - days * 86400))
    h = conn.execute("SELECT median FROM market_history WHERE key=? AND day>=? ORDER BY day", (key, since)).fetchall()
    return (h[-1][0] - h[0][0]) / h[0][0] if len(h) >= 2 and h[0][0] else None


def models(conn, keys: list[str], now: float | None = None) -> dict:
    """Сравнение 2–4 моделей: рынок, динамика, объём, ликвидность, цена за условную производительность."""
    now = time.time() if now is None else now
    keys = list(dict.fromkeys(k for k in keys if k))[:4]
    table = analogs.load()
    cols = []
    for key in keys:
        prices = [r[0] for r in conn.execute(
            "SELECT price FROM listings WHERE item_key=? AND status='active' AND price>0 AND defect=0 AND wanted=0", (key,))]
        liq = db.liquidity(conn, key, now)
        idx = analogs.index_of(key, table)
        median = statistics.median(prices) if prices else None
        cols.append({
            "n": len(prices) or None, "median": median, "min": min(db._trim(sorted(prices))) if prices else None,
            "change7": _change(conn, key, 7, now), "change30": _change(conn, key, 30, now),
            "new7": conn.execute("SELECT COUNT(*) FROM listings WHERE item_key=? AND first_seen>=?", (key, now - 7 * 86400)).fetchone()[0],
            "gone7": conn.execute("SELECT COUNT(*) FROM listings WHERE item_key=? AND status='inactive' AND gone_at>=?",
                                  (key, now - 7 * 86400)).fetchone()[0],
            "liquidity": liq["days"], "_liq_rough": liq["rough"] and liq["days"] is not None,
            "index": idx, "per_index": median / idx if median and idx else None,
            "_group": (analogs.group_of(key, table) or {}).get("name"),
        })
    rows = []
    for k, label, direction, fmt in MODEL_CRITERIA:
        raw = [c[k] for c in cols]
        texts = [fmt(v) for v in raw]
        if k == "liquidity":
            texts = [t + (" (оценка грубая)" if c["_liq_rough"] else "") for t, c in zip(texts, cols)]
        rows.append({"key": k, "label": label, "direction": direction,
                     "cells": [{"text": t, "raw": v, "mark": m, "diff": ""} for t, v, m in zip(texts, raw, _mark(raw, direction))],
                     "differs": len(set(texts)) > 1})
    return {"models": [{"key": k, "group": c["_group"]} for k, c in zip(keys, cols)], "rows": rows,
            "approx": table["approx"], "baseline": table["baseline"], "error": table["error"]}
