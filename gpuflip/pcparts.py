"""Внутренняя стоимость ПК: какие детали названы в объявлении и сколько каждая стоит при быстрой продаже.

Цену не выдумываем: деталь оценивается только по рынку своей позиции (от 6 объявлений) или, для памяти, дисков и
блоков питания, по цене за ГБ / Вт у похожих позиций — с пометкой «приблизительно». Остальное честно помечается
«не оценено», поэтому сумма — нижняя граница, а не полная стоимость.
"""
from __future__ import annotations

import json
import re
import statistics

from . import db, parts
from .config import Config
from .scoring import price_points

KIND_RU = {"gpu": "Видеокарта", "cpu": "Процессор", "mb": "Материнская плата", "ram": "Оперативная память",
           "ssd": "SSD", "hdd": "Жёсткий диск", "disk": "Накопитель", "psu": "Блок питания", "case": "Корпус"}
ORDER = ("gpu", "cpu", "mb", "ram", "ssd", "hdd", "disk", "psu", "case")
EXPECTED = ("gpu", "cpu", "mb", "ram", "disk", "psu")     # что должно быть в любом ПК (корпус по тексту не оценить)
FAMILY_MIN = 10            # от стольких объявлений «родственных» позиций берём цену за ГБ / Вт
PART_SELL_COST = 150.0     # упаковка / встреча на каждую деталь при продаже по частям
STATUS_RU = {"ok": "по рынку позиции", "rough": "мало данных", "approx": "приблизительно",
             "nomarket": "нет рынка", "unknown": "не указано в тексте", "absent": "продавец пишет: нет"}


def _rub(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ") + " ₽"


def _family(part: parts.Part) -> str | None:
    """Шаблон LIKE для «родственных» позиций: та же память / тот же тип диска, но другой объём."""
    words = part.key.split()
    if part.kind == "ram" and len(words) >= 2:
        return f"{words[0]} {words[1]} %"                 # RAM DDR4 %
    if part.kind == "ssd":
        return "SSD % NVMe" if part.key.endswith("NVMe") else "SSD % SATA"
    if part.kind == "hdd":
        return "HDD %"
    return None                                           # у блоков питания цена за ватт от мощности зависит слишком сильно


def _unit_price(conn, pattern: str, now: float) -> tuple[float, int]:
    rows = [r[0] for r in conn.execute(
        "SELECT unit_price FROM listings WHERE item_key LIKE ? AND status='active' AND unit_price>0 AND defect=0 "
        "AND wanted=0 AND junk IS NULL AND last_seen>=?", (pattern, now - 14 * 86400))]
    return (statistics.median(rows), len(rows)) if len(rows) >= FAMILY_MIN else (0.0, len(rows))


SIBLING_DISCOUNT = 0.95   # цена соседней модели процессора берётся с запасом: у F нет графики, K дороже


def cpu_siblings(key: str) -> list[str]:
    """Почти такие же процессоры: i5-13400F ↔ i5-13400 / 13400K / 13400KF, Ryzen 5 5600 ↔ 5600X / 5600XT.
    X3D и G (со встроенной графикой) — другие по цене, их не смешиваем."""
    m = re.fullmatch(r"(CPU i[3579]-\d{4,5})(F|K|KF|KS)?", key)
    if m:
        return [m.group(1) + s for s in ("", "F", "K", "KF") if m.group(1) + s != key]
    m = re.fullmatch(r"(CPU Ryzen [3579] \d{4})(X|XT|F)?", key)
    if m:
        return [m.group(1) + s for s in ("", "X", "XT", "F") if m.group(1) + s != key]
    return []


def _price_part(conn, cfg: Config, part: parts.Part, now: float, caps: dict, cache: dict) -> dict:
    item = {"kind": part.kind, "label": KIND_RU.get(part.kind, part.kind), "key": part.key, "value": None,
            "median": None, "n": 0, "status": "nomarket", "note": ""}
    extra = f"; {part.detail['inferred']}" if part.detail.get("inferred") else ""
    if part.key not in cache:
        cache[part.key] = db.market_stats(conn, part.key, None, now)
    ms = cache[part.key]
    item["n"] = ms.n
    if ms.n >= cfg.min_market_n:
        pts = price_points(ms, cfg, caps.get(part.key))
        item.update(value=round(pts.quick), median=round(ms.median_ask),
                    status="ok" if ms.n >= cfg.alert_market_n else "rough",
                    note=f"{ms.n} объявлений, медиана {_rub(ms.median_ask)}, быстрая продажа — {pts.basis}{extra}")
        return item
    if part.kind == "cpu":
        best = None
        for sib in cpu_siblings(part.key):
            if sib not in cache:
                cache[sib] = db.market_stats(conn, sib, None, now)
            if cache[sib].n >= cfg.min_market_n and (best is None or cache[sib].n > best.n):
                best = cache[sib]
        if best:
            pts = price_points(best, cfg, caps.get(best.key))
            item.update(value=round(pts.quick * SIBLING_DISCOUNT), median=round(best.median_ask), n=best.n, status="approx",
                        note=f"своих объявлений мало ({ms.n}); взята цена почти такого же {best.key.replace('CPU ', '')} "
                             f"({best.n} объявлений) минус 5%")
            return item
    pattern = _family(part)
    if pattern and part.qty:
        fam = ("family", pattern)
        if fam not in cache:
            cache[fam] = _unit_price(conn, pattern, now)
        unit, n = cache[fam]
        if unit:
            item.update(value=round(unit * part.qty * cfg.ask_to_sale), median=round(unit * part.qty), n=n, status="approx",
                        note=f"своей позиции на рынке мало; посчитано по {unit:.1f} {part.unit} у {n} похожих".replace(".", ",", 1) + extra)
            return item
    item["note"] = f"в базе {ms.n} объявлений этой позиции — для оценки мало" if ms.n else "такой позиции в базе нет"
    return item


def breakdown(conn, cfg: Config, title: str, description: str, price: float, now: float,
              flags: dict | None = None, caps: dict | None = None, cache: dict | None = None) -> dict | None:
    """Разбор ПК на детали с оценкой каждой. None — это не настольный ПК (ноутбук, моноблок, неттоп)."""
    b = parts.parse_pc(title, description)
    if b.reject:
        return None
    cache = {} if cache is None else cache
    items = [_price_part(conn, cfg, p, now, caps or {}, cache) for p in b.parts]
    for kind in b.unknown:
        items.append({"kind": kind, "label": KIND_RU.get(kind, kind), "key": "", "value": None, "median": None, "n": 0,
                      "status": "unknown", "note": "корпус по тексту не оценить" if kind == "case" else "в тексте не названо или не распознано"})
    for kind in b.absent:
        items.append({"kind": kind, "label": KIND_RU.get(kind, kind), "key": "", "value": None, "median": None, "n": 0,
                      "status": "absent", "note": "продавец пишет, что этой детали нет"})
    items.sort(key=lambda x: ORDER.index(x["kind"]) if x["kind"] in ORDER else 99)

    priced = [x for x in items if x["value"]]
    disk_kinds = {"ssd", "hdd", "disk"}
    have = {("disk" if x["kind"] in disk_kinds else x["kind"]) for x in priced}
    gone = {("disk" if k in disk_kinds else k) for k in b.absent}
    expected = [k for k in EXPECTED if k not in gone]
    missing = [KIND_RU[k].lower() for k in expected if k not in have]
    value = float(sum(x["value"] for x in priced))
    out = {"items": items, "value": value, "median": float(sum(x["median"] or 0 for x in priced)),
           "priced": len(have & set(expected)), "expected": len(expected), "missing": missing,
           "approx": any(x["status"] in ("approx", "rough") for x in priced),
           "diff": value - price if price and priced else None, "ratio": value / price if price and priced else None,
           "net": None, "days": cfg.bundle_days}
    if price and priced:
        buy = price * (1 - cfg.negotiation_discount)
        risk = min(0.6, sum((flags or {}).values())) * buy * cfg.risk_loss_share
        out["net"] = round(value * (1 - cfg.sell_fee_pct) - buy - cfg.fixed_costs - PART_SELL_COST * len(priced)
                           - buy * cfg.capital_cost_per_day * cfg.bundle_days - risk)
    top = max((x["value"] for x in priced), default=0)
    out["warn"] = ("одна деталь по рынку стоит больше всего ПК — так бывает редко: проверьте, не цена ли это «от», "
                   "не предзаказ и не обман") if price and top >= price else ""
    out["verdict"] = _verdict(out, price)
    return out


def _verdict(out: dict, price: float) -> str:
    if not out["items"] or not any(x["value"] for x in out["items"]):
        return "детали не оценены: в тексте их нет или по ним мало данных"
    tail = f"; не оценено: {', '.join(out['missing'])}" if out["missing"] else ""
    if not price:
        return f"детали ≈ {_rub(out['value'])}{tail}"
    if out["diff"] >= 0:
        return f"оценённые детали дороже лота на {_rub(out['diff'])}{tail}"
    return f"оценённые детали покрывают {out['ratio'] * 100:.0f}% цены{tail}"


def store(conn, cfg: Config, row, now: float, caps: dict | None = None, cache: dict | None = None) -> dict | None:
    """Посчитать разбор для строки listings и сохранить (parts_json, parts_value)."""
    out = breakdown(conn, cfg, row["title"] or "", row["description"] or "", row["price"] or 0.0, now,
                    json.loads(row["flags"] or "{}"), caps, cache)
    value = out["value"] if out and any(x["value"] for x in out["items"]) else None
    conn.execute("UPDATE listings SET parts_json=?, parts_value=? WHERE id=?",
                 (json.dumps(out, ensure_ascii=False) if out else None, value, row["id"]))
    return out
