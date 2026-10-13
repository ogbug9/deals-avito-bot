import json

import pytest

from gpuflip import db, modes
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.notifier import Notifier
from gpuflip.pipeline import process, recheck

NOW = 1_800_000_000.0


def L(i, price, title="Видеокарта RTX 3070 8GB", desc="", city="", delivery=False):
    return RawListing(id=str(i), title=title, price=price, url=f"https://www.avito.ru/x_{i}", description=desc,
                      city=city, delivery=delivery)


def gpu_market(n=12, price=27000):
    return [L(i, price + (i % 3) * 300) for i in range(n)]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    process(c, Config(), gpu_market(), "msk", False, now=NOW, category="videokarty", region="moskva")
    return c


def run(conn, items, cfg=None, category="videokarty", region="moskva", src="msk"):
    out = []
    st = process(conn, cfg or Config(), items, src, False, notifier=Notifier(out=out.append), now=NOW + 60,
                 category=category, region=region)
    return st, out


def tags(conn, i):
    r = conn.execute("SELECT mode_tags FROM listings WHERE id=?", (str(i),)).fetchone()[0]
    return set(r.split(",")) if r else set()


def cand(conn, i, mode):
    return conn.execute("SELECT * FROM candidates WHERE listing_id=? AND mode=?", (str(i), mode)).fetchone()


# --- классика ---
def test_market_candidate_has_reason_and_alert_shows_mode(conn):
    st, out = run(conn, [L(500, 19000)])
    c = cand(conn, 500, "market")
    assert tags(conn, 500) == {"market"} and c["est_profit"] > 5000 and c["confidence"] == "high"
    assert "RTX 3070" in c["reason"] and "n=12" in c["reason"]
    assert st["alerts"] == 1 and "Режим: ниже рынка" in out[0] and "19 000" in out[0]
    assert st["modes"]["market"]["candidates"] == 1


# --- 3. плохое описание ---
def test_badtext_description_and_fuzzy(conn):
    run(conn, [L(501, 19000, "Видеокарта игровая", "продаю rtx 3070 8gb, все отлично"),
               L(502, 19000, "Видеокарта rxt 3O70"),            # fuzzy, но дороже 60% медианы
               L(503, 15000, "Видеокарта rxt 3O70")])           # fuzzy и дёшево
    assert tags(conn, 501) == {"badtext"} and cand(conn, 501, "badtext")["confidence"] == "medium"
    assert tags(conn, 502) == set()
    assert tags(conn, 503) == {"badtext"} and cand(conn, 503, "badtext")["confidence"] == "low"
    assert "опечатк" in cand(conn, 503, "badtext")["reason"]


def test_badtext_mode_can_be_switched_off(conn):
    st, _ = run(conn, [L(501, 19000, "Видеокарта игровая", "продаю rtx 3070 8gb")], Config(mode_badtext=False))
    assert tags(conn, 501) == set() and st["alerts"] == 0


# --- 4. срочные ---
def test_urgent_lowers_threshold_but_needs_marker(conn):
    st, out = run(conn, [L(510, 24100, desc="Срочно, переезд"), L(511, 24100, desc="Отличная карта")])
    assert tags(conn, 510) == {"urgent"} and tags(conn, 511) == set()
    assert 0.10 <= cand(conn, 510, "urgent")["est_profit_pct"] < 0.15
    assert "срочная продажа" in out[0]
    assert st["modes"]["urgent"] == {"viewed": 1, "candidates": 1, "rejected": {}}


def test_urgent_is_not_a_quality_guarantee(conn):
    run(conn, [L(512, 24100, desc="Срочно! не тестировалась, после майнинга"), L(513, 28500, desc="срочно")])
    assert tags(conn, 512) == set()        # риск-штрафы съели прибыль
    assert tags(conn, 513) == set()        # выше медианы — срочность не помогает


# --- 2. регионы ---
def test_regional_needs_delivery_and_uses_moscow_median(conn):
    st, _ = run(conn, [L(520, 21000, city="Казань", delivery=True), L(521, 21000, city="Казань", delivery=False)],
                region="all", src="all")
    assert "regional" in tags(conn, 520) and "regional" not in tags(conn, 521)
    r = cand(conn, 520, "regional")["reason"]
    assert "Казань" in r and "по Москве" in r and "доставка 500" in r
    assert st["modes"]["regional"]["rejected"] == {"no_delivery": 1}
    assert conn.execute("SELECT is_msk FROM listings WHERE id='520'").fetchone()[0] == 0


def test_regional_skips_moscow_sellers(conn):
    st, _ = run(conn, [L(522, 21000, city="Москва", delivery=True)], region="all", src="all")
    assert "regional" not in tags(conn, 522) and "regional" not in st["modes"]


# --- 5. другие категории ---
def test_categories_ram_normalized_per_gb(conn):
    ram = [L(600 + i, 20000 + (i % 3) * 400, "Оперативная память DDR5 64gb (2x32) Kingston Fury 6000") for i in range(12)]
    process(conn, Config(), ram, "ram", False, now=NOW, category="operativnaya_pamyat", region="moskva")
    st, out = run(conn, [L(650, 13000, "ОЗУ DDR5 2x32gb Corsair")], category="operativnaya_pamyat", src="ram")
    row = conn.execute("SELECT item_key, qty, unit_price, brand, gpu_key FROM listings WHERE id='650'").fetchone()
    assert tuple(row) == ("RAM DDR5 64GB", 64.0, 13000 / 64, "corsair", None)
    assert tags(conn, 650) == {"categories"} and "₽/ГБ" in cand(conn, 650, "categories")["reason"]
    assert "Режим: комплектующие" in out[0]


def test_categories_unrecognized_position_is_counted(conn):
    st, _ = run(conn, [L(651, 5000, "Оперативная память")], category="operativnaya_pamyat", src="ram")
    assert st["modes"]["categories"]["rejected"] == {"position_not_recognized": 1}


# --- 1. целые ПК ---
def seed_parts(conn):
    process(conn, Config(), [L(700 + i, 9000 + (i % 3) * 100, "Процессор AMD Ryzen 5 5600") for i in range(12)],
            "cpu", False, now=NOW, category="protsessory", region="moskva")
    process(conn, Config(), [L(730 + i, 3500 + (i % 3) * 50, "Оперативная память DDR4 16gb") for i in range(12)],
            "ram", False, now=NOW, category="operativnaya_pamyat", region="moskva")


def test_bundle_sum_of_parts(conn):
    seed_parts(conn)
    pc = L(800, 25000, "Игровой пк Ryzen 5 5600 RTX 3070 8gb",
           "Оперативная память 16 гб DDR4. Блок питания 650W. SSD 512 гб. Материнская плата B550")
    st, out = run(conn, [pc], category="pc", src="pc")
    c = cand(conn, 800, "bundles")
    assert tags(conn, 800) == {"bundles"} and c["est_profit"] > 10000
    assert "CPU Ryzen 5 5600" in c["reason"] and "RAM DDR4 16GB" in c["reason"] and "RTX 3070" in c["reason"]
    assert "PSU 650W (нет рынка)" in c["reason"] and "корпус" in c["reason"]     # не оценено — цену не выдумываем
    assert c["confidence"] == "low" and "Режим: разбор ПК" in out[0]


def test_bundle_rejections(conn):
    seed_parts(conn)
    st, _ = run(conn, [
        L(801, 25000, "Игровой пк RTX 3070 8gb", "мощный, тянет все"),                    # только видеокарта
        L(802, 38000, "Игровой пк Ryzen 5 5600 RTX 3070 8gb", "озу 16 гб ddr4"),          # детали не на 25% дороже лота
        L(803, 25000, "Игровой пк без видеокарты Ryzen 5 5600", "озу 16 гб ddr4"),
        L(804, 25000, "Ноутбук игровой RTX 3070 Ryzen 5 5600"),
        L(805, 25000, "Игровой пк Ryzen 5 5600 RTX 3070 8gb", "озу 16 гб ddr4. Не включается"),
    ], category="pc", src="pc")
    assert st["candidates"] == 0
    assert st["modes"]["bundles"]["rejected"] == {"only_gpu_recognized": 1, "parts_not_25pct_above_price": 1,
                                                  "gpu_absent": 1, "laptop": 1, "defective": 1}


# --- «проверить вручную» ---
def test_manual_list_is_not_alerted():
    conn = db.connect(":memory:")
    process(conn, Config(), gpu_market(32), "msk", False, now=NOW, category="videokarty", region="moskva")
    st, out = run(conn, [L(900, 5000, "Видеокарта мощная игровая")])
    assert tags(conn, 900) == {"manual"} and st["alerts"] == 0 and not out
    assert "проверить" not in "".join(out) and cand(conn, 900, "manual")["confidence"] == "low"


# --- отчёт и актуальность ---
def test_by_mode_report_and_recheck(conn):
    run(conn, [L(500, 19000), L(510, 24100, desc="срочно")])
    states = {"https://www.avito.ru/x_500": "inactive", "https://www.avito.ru/x_510": "active"}
    res = recheck(conn, Config(recheck_per_cycle=5), lambda url: {"state": states[url], "description": "", "condition": "Б/у"},
                  now=NOW + 7 * 3600)
    assert res == {"checked": 2, "inactive": 1, "unknown": 0}
    assert conn.execute("SELECT status FROM listings WHERE id='500'").fetchone()[0] == "inactive"
    assert conn.execute("SELECT avito_condition FROM listings WHERE id='510'").fetchone()[0] == "Б/у"
    rep = {r["mode"]: r for r in modes.by_mode_report(conn)}
    assert rep["market"]["candidates"] == 1 and rep["market"]["inactive_share"] == 1.0
    assert rep["urgent"]["candidates"] == 1 and rep["urgent"]["inactive_share"] == 0.0
    assert rep["market"]["viewed"] >= 13 and rep["market"]["avg_profit"] > 5000
    # ушедшее объявление больше не участвует в медиане
    assert db.market_stats(conn, "RTX 3070", None, NOW + 8 * 3600).n == 13


def test_recheck_unknown_page_changes_nothing(conn):
    run(conn, [L(500, 19000)])
    res = recheck(conn, Config(), lambda url: {"state": "unknown"}, now=NOW + 7 * 3600)
    assert res["unknown"] == 1
    assert conn.execute("SELECT status, checked_at FROM listings WHERE id='500'").fetchone()[:] == ("active", None)


def test_candidate_tag_removed_when_price_rises(conn):
    run(conn, [L(500, 19000)])
    assert tags(conn, 500) == {"market"}
    process(conn, Config(), [L(500, 27000)], "msk", False, now=NOW + 120, category="videokarty", region="moskva")
    assert tags(conn, 500) == set()
    assert conn.execute("SELECT COUNT(*) FROM price_history WHERE listing_id='500'").fetchone()[0] == 2


# --- перепроверка карточки и план источников ---
def test_parse_listing_page_states():
    from gpuflip.collector import parse_listing_page
    live = ('<html><title>RTX 3070</title><div data-marker="item-view/item-price">28 000</div>'
            '<div data-marker="item-view/item-description">Полное описание карты</div><li>Состояние: Б/у</li></html>')
    assert parse_listing_page(200, live) == {"state": "active", "description": "Полное описание карты", "condition": "Б/у"}
    assert parse_listing_page(404, "")["state"] == "inactive"
    assert parse_listing_page(200, '<div data-marker="item-view/closed-warning">Объявление снято с публикации</div>')["state"] == "inactive"
    assert parse_listing_page(429, "<title>Доступ ограничен: проблема с IP</title>")["state"] == "unknown"
    assert parse_listing_page(200, "<html><body>что-то незнакомое</body></html>")["state"] == "unknown"


def test_modes_and_regions_from_env(tmp_path, monkeypatch):
    from gpuflip.cli import source_plan
    for k in ("MODE_BUNDLES", "REGIONS", "DELIVERY_COST"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("MODE_BUNDLES=0\nMODE_URGENT=1\nREGIONS=kazan, sankt-peterburg\nDELIVERY_COST=700\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    c = Config.load(str(env))
    assert not c.mode_bundles and c.mode_urgent and c.regions == ["kazan", "sankt-peterburg"] and c.delivery_cost == 700
    plan = source_plan(c)
    urls = [u for _, u, _, cat, reg in plan if reg == "kazan"]
    assert len(urls) == 1 and "/kazan/" in urls[0] and "d=1" in urls[0]
    assert all(pages == c.region_max_pages for _, _, pages, _, reg in plan if reg in ("kazan", "sankt-peterburg"))
    c.mode_regional = False
    assert not [1 for *_, reg in source_plan(c) if reg == "kazan"]
