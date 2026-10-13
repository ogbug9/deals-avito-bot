"""Фильтры списка объявлений → SQL. Используется интерфейсом, экспортом и уведомлениями по сохранённым поискам."""
from __future__ import annotations

import time

from . import modes, sources

USER_STATUSES = ("new", "checked", "bought", "skipped")
STALE_DAYS = 3.0
SORTS = {
    "date": "COALESCE(posted_at, first_seen) DESC",
    "profit": "est_profit IS NULL, est_profit DESC",
    "pct": "json_extract(score_json, '$.discount_vs_market') IS NULL, json_extract(score_json, '$.discount_vs_market') DESC",
    "price_asc": "price ASC",
    "price_desc": "price DESC",
    "rating": "rating IS NULL, rating DESC, reviews DESC",
    "drop": "(first_price - price) DESC",
    "age": "COALESCE(posted_at, first_seen) ASC",
    "title": "lc(title) ASC",
    "n": "json_extract(score_json, '$.market_n') IS NULL, json_extract(score_json, '$.market_n') DESC",
    "median": "json_extract(score_json, '$.median_ask') IS NULL, json_extract(score_json, '$.median_ask') DESC",
    "city": "city IS NULL OR city='', lc(city) ASC",
    "model": "item_key IS NULL, item_key ASC",
    "first": "first_seen DESC",
    "seen": "last_seen DESC",
    "gone": "gone_at IS NULL, gone_at DESC",
    "life": "gone_at IS NULL, (gone_at - COALESCE(posted_at, first_seen)) ASC",
}
# подписи фильтров — для чипов и подсказки «какой фильтр обнулил выдачу»
FILTER_LABELS = {"q": "поиск по тексту", "cat": "категория", "pmin": "цена от", "pmax": "цена до", "region": "регион",
                 "city": "город или метро", "delivery": "только с доставкой", "rating": "рейтинг продавца",
                 "age": "свежесть, дней", "hours": "свежесть, часов", "model": "модель или позиция",
                 "profit": "только кандидаты", "mode": "режим поиска", "fav": "только избранное",
                 "ustatus": "мой статус", "conf": "доверие", "disc": "скидка к рынку от, %", "urgent": "срочные",
                 "drop": "цена снижена", "ids": "выбранные лоты",
                 "status": "какие объявления"}


def _one(q: dict, k: str) -> str:
    return (q.get(k, [""])[0] or "").strip()


def _int(q: dict, name: str, default: int = 0) -> int:
    try:
        return int(float(q.get(name, [""])[0] or default))
    except ValueError:
        return default


def order_by(q: dict) -> str:
    """ORDER BY из параметров sort и dir (dir=flip разворачивает направление)."""
    sql = SORTS.get(_one(q, "sort") or "date", SORTS["date"])
    if _one(q, "dir") == "flip":
        head, _, tail = sql.rpartition(",")
        last = tail.strip()
        last = last[:-4] + "ASC" if last.endswith("DESC") else last[:-3] + "DESC" if last.endswith("ASC") else last
        sql = f"{head}, {last}" if head else last
    return sql


def build_query(q: dict, conn=None) -> tuple[str, list]:
    """Параметры запроса → (WHERE, аргументы). q — как из parse_qs. conn нужен, чтобы учесть скрытых продавцов и модели."""
    where, args = ["1=1"], []
    life = _one(q, "status")
    if life == "inactive":                     # сняты с публикации (подтверждено карточкой)
        where.append("status='inactive'")
    elif life == "stale":                      # числятся активными, но давно не попадались в выдаче
        where.append("status='active' AND last_seen<?")
        args.append(time.time() - STALE_DAYS * 86400)
    elif life != "all":
        where.append("status='active'")
    cats = [c for c in _one(q, "cat").split(",") if c in sources.CATEGORIES]
    if cats:
        where.append(f"category IN ({','.join('?' * len(cats))})")
        args += cats
    if _one(q, "region") == "msk":
        where.append("is_msk=1")
    elif _one(q, "region") == "regions":
        where.append("is_msk=0")
    elif _one(q, "region"):
        where.append("region=?")
        args.append(_one(q, "region"))
    if _int(q, "pmin"):
        where.append("price>=?")
        args.append(_int(q, "pmin"))
    if _int(q, "pmax"):
        where.append("price<=?")
        args.append(_int(q, "pmax"))
    if _one(q, "delivery") == "1":
        where.append("delivery=1")
    if _one(q, "defect") != "show":            # по умолчанию брак скрыт
        where.append("defect=0")
    if _one(q, "wanted") != "show":
        where.append("wanted=0")
    if _one(q, "model"):
        where.append("(item_key=? OR fine_key=?)")
        args += [_one(q, "model")] * 2
    if _one(q, "profit") == "1":
        where.append("mode_tags IS NOT NULL AND mode_tags<>'manual'")
    if _one(q, "mode") in modes.MODE_TITLES:
        where.append("(',' || mode_tags || ',') LIKE ?")
        args.append(f"%,{_one(q, 'mode')},%")
    if _one(q, "conf") in modes.CONFIDENCE_RU:
        where.append("confidence=?")
        args.append(_one(q, "conf"))
    if _one(q, "fav") == "1":
        where.append("fav=1")
    if _one(q, "urgent") == "1":
        where.append("urgent=1")
    if _one(q, "drop") == "1":
        where.append("first_price>price AND price>0")
    if _int(q, "disc"):
        where.append("json_extract(score_json, '$.discount_vs_market')>=? AND json_extract(score_json, '$.market_n')>=6")
        args.append(_int(q, "disc") / 100)
    if _one(q, "ustatus") == "none":
        where.append("user_status IS NULL")
    elif _one(q, "ustatus") in USER_STATUSES:
        where.append("user_status=?")
        args.append(_one(q, "ustatus"))
    elif _one(q, "skipped") != "show":         # «пропустил» — значит больше не показывать
        where.append("(user_status IS NULL OR user_status<>'skipped')")
    if _one(q, "rating"):
        try:
            args.append(float(_one(q, "rating")))
            where.append("rating>=?")
        except ValueError:
            pass
    if _int(q, "age"):
        where.append("COALESCE(posted_at, first_seen)>=?")
        args.append(time.time() - _int(q, "age") * 86400)
    if _int(q, "hours"):
        where.append("COALESCE(posted_at, first_seen)>=?")
        args.append(time.time() - _int(q, "hours") * 3600)
    if _one(q, "city"):
        where.append("lc(city) LIKE ?")
        args.append(f"%{_one(q, 'city').lower()}%")
    if _one(q, "seller"):
        where.append("seller_id=?")
        args.append(_one(q, "seller"))
    if _one(q, "ids"):
        ids = [i for i in _one(q, "ids").split(",") if i][:200]
        where.append(f"id IN ({','.join('?' * len(ids))})")
        args += ids
    for word in _one(q, "q").lower().split()[:8]:
        where.append("lc(title || ' ' || COALESCE(description, '')) LIKE ?")
        args.append(f"%{word}%")
    if conn is not None and _one(q, "excluded") != "show" and not _one(q, "ids"):
        where.append("(seller_id IS NULL OR seller_id NOT IN (SELECT value FROM excluded WHERE kind='seller'))")
        where.append("(item_key IS NULL OR item_key NOT IN (SELECT value FROM excluded WHERE kind='model'))")
    return " AND ".join(where), args
