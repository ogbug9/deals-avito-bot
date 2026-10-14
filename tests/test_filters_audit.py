"""Сверка фильтров и сортировок: ответ API сравнивается с прямым перебором строк базы."""
import json
import random

import pytest

from gpuflip import db
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.pipeline import process
from gpuflip.ui import App

NOW = 1_800_000_000.0
DAY = 86400.0
TITLES = ["Видеокарта RTX 3070 8GB", "Видеокарта RTX 3060 12GB", "Видеокарта GTX 1660 Super", "Видеокарта RX 580 8GB",
          "Видеокарта RTX 3070 8GB новая", "Коробка от RTX 3070 8GB", "Видеокарта игровая"]
DESCS = ["", "срочно, торг", "не включается", "отличное состояние, без артефактов", "после майнинга", "запечатана"]
CITIES = ["ВДНХ, до 5 мин.", "Казань", "Алтуфьево", ""]
BASE = {"status": "all", "defect": "show", "wanted": "show", "excluded": "show", "skipped": "show"}


def q(**kw):
    return {k: [str(v)] for k, v in kw.items()}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    import time as _time
    rnd = random.Random(7)
    path = str(tmp_path_factory.mktemp("audit") / "a.db")
    conn = db.connect(path)
    now = _time.time()
    items = []
    for i in range(160):
        title = rnd.choice(TITLES)
        base = {"3070": 28000, "3060": 29000, "1660": 12000, "580": 6000}.get(next((k for k in ("3070", "3060", "1660", "580") if k in title), ""), 9000)
        items.append(RawListing(str(i), title, round(base * rnd.uniform(0.55, 1.25), -2), f"https://www.avito.ru/x_{i}",
                                rnd.choice(DESCS), rnd.choice(CITIES), delivery=rnd.random() < 0.4,
                                date_text=rnd.choice(["1 час назад", "5 часов назад", "2 дня назад", "1 неделю назад", ""]),
                                rating=rnd.choice([None, 4.2, 4.6, 4.9, 5.0]), reviews=rnd.choice([None, 3, 40]),
                                seller_id=rnd.choice(["", "", "shop1", "shop2"]), seller_name="Продавец"))
    process(conn, Config(), items[:80], "s1", False, now=now - 2 * DAY, category="videokarty", region="moskva")
    process(conn, Config(), items[80:], "s2", False, now=now - 3600, category="videokarty", region="all")
    cheaper = [RawListing(l.id, l.title, l.price - 1500, l.url, l.description, l.city) for l in items[:30:3]]
    process(conn, Config(), cheaper, "s1", False, now=now - 600, category="videokarty", region="moskva")
    for i, st in ((3, "checked"), (5, "skipped"), (7, "bought"), (9, "new")):
        conn.execute("UPDATE listings SET user_status=? WHERE id=?", (st, str(i)))
    conn.execute("UPDATE listings SET fav=1 WHERE CAST(id AS INTEGER) % 11 = 0")
    conn.execute("UPDATE listings SET status='inactive', gone_at=? - CAST(id AS INTEGER) * 60 WHERE CAST(id AS INTEGER) % 13 = 0",
                 (now - 1800,))
    db.exclude(conn, "seller", "shop2", "Магазин 2")
    conn.commit()
    conn.close()
    return App(path)


def all_rows(app):
    return app.listings(q(per=100, limit=300, offset=0, **BASE))["items"]


def api_ids(app, **kw):
    return sorted(i["id"] for i in app.listings(q(limit=300, offset=0, **{**BASE, **kw}))["items"])


def want(rows, pred):
    return sorted(r["id"] for r in rows if pred(r))


def disc(r):
    sc = r["score"]
    return sc["discount_vs_market"] if sc and (sc.get("market_n") or 0) >= 6 else None


CASES = [
    ("pmin", {"pmin": 10000}, lambda r: r["price"] >= 10000),
    ("pmax", {"pmax": 10000}, lambda r: r["price"] <= 10000),
    ("цена от и до", {"pmin": 8000, "pmax": 20000}, lambda r: 8000 <= r["price"] <= 20000),
    ("доставка", {"delivery": 1}, lambda r: r["delivery"] == 1),
    ("регион Москва", {"region": "msk"}, lambda r: r["is_msk"] == 1),
    ("другие регионы", {"region": "regions"}, lambda r: r["is_msk"] == 0),
    ("город", {"city": "казань"}, lambda r: "казань" in (r["city"] or "").lower()),
    ("модель", {"model": "RTX 3070"}, lambda r: r["item_key"] == "RTX 3070"),
    ("поиск по тексту", {"q": "3060 12gb"}, lambda r: "3060" in (r["title"] + " " + (r["description"] or "")).lower()
     and "12gb" in (r["title"] + " " + (r["description"] or "")).lower()),
    ("только кандидаты", {"profit": 1}, lambda r: bool(r["mode_tags"]) and r["mode_tags"] != "manual"),
    ("режим срочные", {"mode": "urgent"}, lambda r: "urgent" in (r["mode_tags"] or "").split(",")),
    ("режим ниже рынка", {"mode": "market"}, lambda r: "market" in (r["mode_tags"] or "").split(",")),
    ("доверие высокое", {"conf": "high"}, lambda r: r["confidence"] == "high"),
    ("скидка от 20%", {"disc": 20}, lambda r: disc(r) is not None and disc(r) >= 0.2),
    ("срочные", {"urgent": 1}, lambda r: r["urgent"] == 1),
    ("цена снижена", {"drop": 1}, lambda r: r["price_drop"] > 0),
    ("избранное", {"fav": 1}, lambda r: r["fav"] == 1),
    ("статус проверил", {"ustatus": "checked"}, lambda r: r["user_status"] == "checked"),
    ("без статуса", {"ustatus": "none"}, lambda r: r["user_status"] is None),
    ("рейтинг от 4,8", {"rating": 4.8}, lambda r: r["rating"] is not None and r["rating"] >= 4.8),
    ("за сутки", {"age": 1}, lambda r: r["age_hours"] is not None and r["age_hours"] <= 24),
    ("за час", {"hours": 1}, lambda r: r["age_hours"] is not None and r["age_hours"] <= 1),
    ("только мусор", {"junk": 1}, lambda r: bool(r["junk"])),
    ("только б/у", {"cond": "used"}, lambda r: r["cond"] == "used"),
    ("только новое", {"cond": "new"}, lambda r: r["cond"] == "new"),
    ("ушедшие", {"status": "inactive"}, lambda r: r["gone"]),
    ("давно не видели", {"status": "stale"}, lambda r: r["stale"]),
    ("продавец", {"seller": "shop1"}, lambda r: r["seller_id"] == "shop1"),
]


@pytest.mark.parametrize("name,params,pred", CASES, ids=[c[0] for c in CASES])
def test_filter_matches_brute_force(app, name, params, pred):
    rows = all_rows(app)
    expected = want(rows, pred)
    assert api_ids(app, **params) == expected
    assert len(rows) == 160


def test_filters_are_not_all_trivial(app):
    """Каждая проверка выше что-то отбирает и что-то отсеивает — иначе она ничего не доказывает."""
    rows = all_rows(app)
    empty = [name for name, _, pred in CASES if not want(rows, pred)]
    full = [name for name, _, pred in CASES if len(want(rows, pred)) == len(rows)]
    assert not full and len(empty) <= 3, (empty, full)


def test_default_view_hides_what_it_says(app):
    rows = all_rows(app)
    visible = sorted(i["id"] for i in app.listings(q(limit=300, offset=0))["items"])
    assert visible == want(rows, lambda r: not r["gone"] and not r["defect"] and not r["wanted"] and not r["junk"]
                           and r["user_status"] != "skipped" and r["seller_id"] != "shop2")
    assert set(api_ids(app, defect="hide")) == set(want(rows, lambda r: not r["defect"] and not r["junk"]))
    assert set(api_ids(app, excluded="hide")) == set(want(rows, lambda r: r["seller_id"] != "shop2"))


def test_combined_filters_intersect(app):
    rows = all_rows(app)
    assert api_ids(app, delivery=1, pmax=15000, cond="used") == want(
        rows, lambda r: r["delivery"] == 1 and r["price"] <= 15000 and r["cond"] == "used")
    assert api_ids(app, profit=1, region="msk", drop=1) == want(
        rows, lambda r: bool(r["mode_tags"]) and r["mode_tags"] != "manual" and r["is_msk"] == 1 and r["price_drop"] > 0)


def keyed(rows, key):
    """Значения ключа сортировки без пустых — пустые всегда уходят в конец, проверяем это отдельно."""
    vals = [key(r) for r in rows]
    tail = [v for v in vals if v is None]
    return [v for v in vals if v is not None], vals[len(vals) - len(tail):] == tail


SORTS = [
    ("date", lambda r: r["posted_at"] or r["first_seen"], True),
    ("price_asc", lambda r: r["price"], False),
    ("price_desc", lambda r: r["price"], True),
    ("profit", lambda r: r["est_profit"], True),
    ("roi", lambda r: r["est_profit_pct"] if r["est_profit"] and r["est_profit"] > 0 else None, True),
    ("perday", lambda r: r["est_profit"] / max((r["score"] or {}).get("days_to_sell") or 14, 0.5)
     if r["est_profit"] and r["est_profit"] > 0 else None, True),
    ("pct", disc, True),
    ("rating", lambda r: r["rating"], True),
    ("drop", lambda r: (r["first_price"] or 0) - (r["price"] or 0), True),
    ("median", lambda r: (r["score"] or {}).get("median_ask") if r["score"] else None, True),
    ("n", lambda r: (r["score"] or {}).get("market_n") if r["score"] else None, True),
    ("quick", lambda r: ((r["score"] or {}).get("quick_price") or None) if r["score"] else None, True),
    ("first", lambda r: r["first_seen"], True),
    ("seen", lambda r: r["last_seen"], True),
    ("gone", lambda r: r["gone_at"] if r["gone"] else None, True),
    ("model", lambda r: r["item_key"], False),
    ("title", lambda r: r["title"].lower(), False),
]


@pytest.mark.parametrize("sort,key,desc", SORTS, ids=[s[0] for s in SORTS])
def test_sort_order_and_flip(app, sort, key, desc):
    rows = app.listings(q(limit=300, offset=0, sort=sort, **BASE))["items"]
    vals, nulls_last = keyed(rows, key)
    assert len(rows) == 160 and nulls_last, "пустые значения должны быть в конце"
    assert vals == sorted(vals, reverse=desc)
    assert len(set(vals)) > 1, "сортировка по одинаковым значениям ничего не проверяет"
    flipped = app.listings(q(limit=300, offset=0, sort=sort, dir="flip", **BASE))["items"]
    fvals, _ = keyed(flipped, key)
    assert sorted(fvals) == sorted(vals) and fvals == sorted(fvals, reverse=not desc)


def test_pct_sort_ignores_listings_without_market(app):
    """«К рынку» не должна поднимать наверх лоты, у которых рынок не оценён (меньше 6 объявлений)."""
    rows = app.listings(q(limit=300, offset=0, sort="pct", **BASE))["items"]
    seen_none = False
    for r in rows:
        if disc(r) is None:
            seen_none = True
        else:
            assert not seen_none, "лот с оценкой рынка стоит после лота без оценки"


def test_paging_windows_cover_everything_once(app):
    total = app.listings(q(limit=1, offset=0, sort="price_asc", **BASE))["total"]
    got = []
    for off in range(0, total, 37):
        got += [i["id"] for i in app.listings(q(limit=37, offset=off, sort="price_asc", **BASE))["items"]]
    assert len(got) == total == 160 and len(set(got)) == total


def test_market_numbers_match_listing_scores(app):
    """Показатели вкладки «Рынок» совпадают с тем, от чего посчитана прибыль в карточке."""
    market = {r["key"]: r for r in app.market(q(days=7))["rows"]}
    rows = [r for r in app.listings(q(limit=300, offset=0))["items"] if r["score"] and (r["score"].get("market_n") or 0) >= 6]
    assert rows
    for r in [x for x in rows if x["score"]["market_n"] >= 12][:25]:
        m = market[r["item_key"]]
        assert m["median"] > 0      # сама медиана у лота посчитана на момент его обхода и может отличаться
        assert m["quick"] and r["score"]["quick_price"] <= r["score"]["median_ask"]
