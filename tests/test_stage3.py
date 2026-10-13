"""Этап 3: глубокая карточка, срок продажи, история цены, перекупщики, журнал сделок, бэкапы."""
import json
import os
import time

import pytest

from gpuflip import db
from gpuflip.collector import RawListing
from gpuflip.config import Config, set_env
from gpuflip.notifier import Notifier
from gpuflip.pipeline import deep_targets, process, recheck

NOW = 1_800_000_000.0
DAY = 86400.0


def L(i, price, title="Видеокарта RTX 3070 8GB", desc="", **kw):
    return RawListing(id=str(i), title=title, price=price, url=f"https://www.avito.ru/x_{i}", description=desc, **kw)


def market(n=12, price=27000, start=0):
    return [L(start + i, price + (i % 3) * 300) for i in range(n)]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    process(c, Config(), market(), "msk", False, now=NOW, category="videokarty", region="moskva")
    return c


def one(conn, i, cols):
    return conn.execute(f"SELECT {cols} FROM listings WHERE id=?", (str(i),)).fetchone()


# ---------------------------------------------------------------- 3. глубокая карточка
def test_deep_targets_pass_price_prefilter_only(conn):
    process(conn, Config(), [L(500, 22000), L(501, 26900), L(502, 22000, desc="не включается"), L(503, 90000)],
            "msk", False, now=NOW + 60, category="videokarty", region="moskva")
    assert [r["id"] for r in deep_targets(conn, Config(), NOW + 120, 10)] == ["500"]     # 501 — почти по рынку


def test_deep_card_is_cached_and_reread_after_price_change(conn):
    process(conn, Config(), [L(500, 22000)], "msk", False, now=NOW + 60, category="videokarty", region="moskva")
    calls = []

    def check(url):
        calls.append(url)
        return {"state": "active", "description": "Полное описание: карта из личного ПК, не майнила", "condition": "Б/у",
                "photos": ["https://img/1.jpg", "https://img/2.jpg"], "seller_name": "Иван", "seller_type": "Частное лицо",
                "rating": 4.8, "avito_model": "MSI GeForce RTX 3070"}
    res = recheck(conn, Config(recheck_per_cycle=0), check, now=NOW + 120)
    assert res["deep"] == 1 and len(calls) == 1
    r = one(conn, 500, "description, avito_condition, photos, seller_name, seller_type, rating, avito_model, deep_at")
    assert r["description"].startswith("Полное описание") and r["avito_condition"] == "Б/у" and r["deep_at"] == NOW + 120
    assert json.loads(r["photos"]) == ["https://img/1.jpg", "https://img/2.jpg"]
    assert (r["seller_name"], r["seller_type"], r["rating"], r["avito_model"]) == ("Иван", "Частное лицо", 4.8, "MSI GeForce RTX 3070")
    recheck(conn, Config(recheck_per_cycle=0), check, now=NOW + 180)
    assert len(calls) == 1                                    # кэш: второй раз карточку не открываем
    process(conn, Config(), [L(500, 21000)], "msk", False, now=NOW + 240, category="videokarty", region="moskva")
    recheck(conn, Config(recheck_per_cycle=0), check, now=NOW + 300)
    assert len(calls) == 2                                    # цена изменилась — перечитали


def test_deep_card_full_description_can_reveal_defect(conn):
    process(conn, Config(), [L(500, 19000)], "msk", False, now=NOW + 60, category="videokarty", region="moskva")
    assert one(conn, 500, "mode_tags")[0] == "market"
    recheck(conn, Config(recheck_per_cycle=0),
            lambda url: {"state": "active", "description": "Отличная карта. В конце: иногда артефакты под нагрузкой"},
            now=NOW + 120)
    assert tuple(one(conn, 500, "defect, mode_tags")) == (1, None)


# ---------------------------------------------------------------- 4. реальный срок продажи
def test_lifetimes_and_liquidity(conn):
    assert db.liquidity(conn, "RTX 3070", NOW)["label"] == "нет данных"
    fresh = [L(600 + i, 27000, date_text="2 часа назад") for i in range(4)]
    process(conn, Config(), fresh, "msk", False, now=NOW + DAY, category="videokarty", region="moskva")
    for i, days in enumerate((2, 4, 6, 20)):                 # ушли через 2, 4, 6 и 20 дней после публикации
        conn.execute("UPDATE listings SET status='inactive', gone_at=posted_at+? WHERE id=?", (days * DAY, str(600 + i)))
    liq = db.liquidity(conn, "RTX 3070", NOW + 30 * DAY)
    assert liq["days"] == 5.0 and liq["n"] == 4 and liq["label"] == "высокая" and liq["rough"] is True
    st = db.market_stats(conn, "RTX 3070", None, NOW + 30 * DAY)
    assert st.days_to_sell == 5.0 and st.lived_n == 4


def test_baseline_listing_without_date_is_not_counted(conn):
    conn.execute("UPDATE listings SET status='inactive', gone_at=first_seen+5*86400 WHERE id IN ('0','1','2')")
    assert db.market_stats(conn, "RTX 3070", None, NOW + 10 * DAY).lived_n == 0      # возраст неизвестен
    assert db.liquidity(conn, "RTX 3070", NOW + 10 * DAY)["level"] == "none"


def test_real_days_replace_default_in_score(conn):
    fresh = [L(600 + i, 27000, date_text="1 час назад") for i in range(3)]
    process(conn, Config(), fresh, "msk", False, now=NOW + DAY, category="videokarty", region="moskva")
    conn.execute("UPDATE listings SET status='inactive', gone_at=posted_at+3*86400 WHERE id IN ('600','601','602')")
    process(conn, Config(), [L(500, 19000)], "msk", False, now=NOW + 5 * DAY, category="videokarty", region="moskva")
    sc = json.loads(one(conn, 500, "score_json")[0])
    assert sc["days_real"] and sc["days_to_sell"] == 3.0 and sc["days_n"] == 3
    process(conn, Config(), [L(501, 19000, "Видеокарта RTX 3060 Ti 8GB")], "msk", False, now=NOW + 5 * DAY,
            category="videokarty", region="moskva")
    assert json.loads(one(conn, 501, "score_json")[0])["days_real"] is False


# ---------------------------------------------------------------- 5. история цены
def test_price_history_records_changes_only(conn):
    for t, price in ((60, 25000), (120, 25000), (180, 23000), (240, 21500)):
        process(conn, Config(), [L(500, price)], "msk", False, now=NOW + t, category="videokarty", region="moskva")
    hist = conn.execute("SELECT ts, price FROM price_history WHERE listing_id='500' ORDER BY ts").fetchall()
    assert [(h["ts"] - NOW, h["price"]) for h in hist] == [(60, 25000), (180, 23000), (240, 21500)]
    assert tuple(one(conn, 500, "first_price, price")) == (25000, 21500)


# ---------------------------------------------------------------- 6. перекупщики и дубли
def test_reseller_flag_and_penalty(conn):
    shop = [L(700 + i, 26000, seller_id="shop1", seller_name="Вик Пик") for i in range(3)]
    solo = L(710, 26000, seller_id="ivan", seller_name="Иван")
    process(conn, Config(), shop + [solo], "msk", False, now=NOW + 60, category="videokarty", region="moskva")
    process(conn, Config(), shop + [solo], "msk", False, now=NOW + 120, category="videokarty", region="moskva")
    r = one(conn, 700, "reseller, seller_ads, flags")
    assert r["reseller"] == 1 and r["seller_ads"] == 3 and json.loads(r["flags"]) == {"reseller": 0.10}
    assert tuple(one(conn, 710, "reseller, flags")) == (0, "{}")
    a, b = (json.loads(one(conn, i, "score_json")[0])["net"] for i in (700, 710))
    assert a < b                                             # тот же лот у перекупщика оценивается ниже


def test_duplicate_listings_without_seller_id_are_flagged(conn):
    dups = [L(720 + i, 15555, "Видеокарта RTX 3070 8GB новая", "В наличии 10 штук, гарантия, доставка по России") for i in range(3)]
    same_title = [L(730 + i, 27000, "Видеокарта RTX 3070 8GB") for i in range(3)]     # типовой заголовок — не дубли
    process(conn, Config(), same_title, "msk", False, now=NOW + 30, category="videokarty", region="moskva")
    assert one(conn, 730, "reseller")[0] == 0
    process(conn, Config(), dups, "msk", False, now=NOW + 60, category="videokarty", region="moskva")
    process(conn, Config(), dups, "msk", False, now=NOW + 120, category="videokarty", region="moskva")
    assert one(conn, 720, "reseller")[0] == 1


def test_excluded_seller_and_model_get_no_alerts(conn):
    out = []
    db.exclude(conn, "seller", "badshop", "Плохой магазин")
    process(conn, Config(), [L(500, 19000, seller_id="badshop"), L(501, 19000)], "msk", False,
            notifier=Notifier(out=out.append), now=NOW + 60, category="videokarty", region="moskva")
    assert len(out) == 1 and "x_501" in out[0]
    db.exclude(conn, "model", "RTX 3070")
    out.clear()
    process(conn, Config(), [L(502, 19000)], "msk", False, notifier=Notifier(out=out.append), now=NOW + 120,
            category="videokarty", region="moskva")
    assert out == [] and db.excluded(conn) == {"seller": {"badshop"}, "model": {"RTX 3070"}}
    db.exclude(conn, "model", "RTX 3070", on=False)
    assert db.excluded(conn)["model"] == set()


# ---------------------------------------------------------------- 7. журнал сделок
def test_deals_journal_roundtrip(conn):
    process(conn, Config(), [L(500, 19000)], "msk", False, now=NOW + 60, category="videokarty", region="moskva")
    est = one(conn, 500, "est_profit")[0]
    did = db.deal_create(conn, "500", buy_price=18000, costs=300, note="самовывоз", bought_at=NOW)
    assert one(conn, 500, "user_status")[0] == "bought"
    j = db.deals_list(conn, now=NOW + 2 * DAY)
    d = j["deals"][0]
    assert (d["title"], d["item_key"], d["buy_price"], d["closed"], d["days"]) == ("Видеокарта RTX 3070 8GB", "RTX 3070", 18000, False, 2.0)
    assert d["est_profit"] == est and d["net"] is None
    assert j["summary"]["open"] == 1 and j["summary"]["open_money"] == 18300 and j["summary"]["roi"] is None
    db.deal_update(conn, did, sell_price=25000, sold_at=NOW + 5 * DAY, costs=700)
    j = db.deals_list(conn, now=NOW + 9 * DAY)
    d, s = j["deals"][0], j["summary"]
    assert d["closed"] and d["days"] == 5.0 and d["net"] == 25000 - 18000 - 700
    assert d["roi"] == pytest.approx(6300 / 18700) and d["est_gap"] == pytest.approx(6300 - est)
    assert (s["closed"], s["net"], s["invested"], s["avg_days"]) == (1, 6300, 18700, 5.0)
    assert s["est_total"] == est and s["real_for_est"] == 6300
    assert db.deals_list(conn, since=NOW + DAY)["deals"] == []           # период: куплено раньше
    assert db.deal_stats(conn)["closed"] == 1
    assert db.deal_delete(conn, did) and db.deals_list(conn)["deals"] == []


def test_manual_deal_without_listing_and_validation(conn):
    did = db.deal_create(conn, None, buy_price=5000, title="RX 580 с рук")
    assert db.deals_list(conn)["deals"][0]["title"] == "RX 580 с рук" and did > 0
    with pytest.raises(ValueError):
        db.deal_create(conn, None, buy_price=0)
    assert db.deal_update(conn, did, hacker_field=1) is False


# ---------------------------------------------------------------- F. бэкапы, настройки, .env
def test_auto_backup_daily_with_rotation(tmp_path):
    path = str(tmp_path / "g.db")
    db.connect(path).close()
    made = []
    for day in range(10):
        b = db.auto_backup(path, keep=7, now=NOW + day * DAY)
        if b:
            os.utime(b, (NOW + day * DAY, NOW + day * DAY))
            made.append(b)
        assert db.auto_backup(path, keep=7, now=NOW + day * DAY + 3600) is None     # в те же сутки — не чаще раза
    files = sorted(p.name for p in (tmp_path / "backups").glob("auto-*.db"))
    assert len(made) == 10 and len(files) == 7 and files[-1] in made[-1]


def test_migration_from_v3_keeps_data_and_makes_backup(tmp_path, capsys):
    path = str(tmp_path / "old.db")
    c = db.connect(path)
    process(c, Config(), market(3), "msk", False, now=NOW, category="videokarty", region="moskva")
    c.execute("PRAGMA user_version=3")
    c.commit()
    c.close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert c.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3
    assert len(list((tmp_path / "backups").glob("old-*.db"))) == 1
    assert "копия" in capsys.readouterr().out


def test_settings_and_env_file(tmp_path):
    c = db.connect(":memory:")
    db.set_setting(c, "ui.view", {"mode": "table", "cols": ["price", "title"]})
    assert db.get_setting(c, "ui.view")["mode"] == "table" and db.get_setting(c, "нет", 5) == 5
    env = tmp_path / ".env"
    env.write_text("# комментарий\nTG_TOKEN=\nMAX_PRICE=40000   # потолок\n", encoding="utf-8")
    set_env(str(env), {"TG_TOKEN": "123:abc", "TG_CHAT_ID": "42"})
    assert env.read_text(encoding="utf-8") == "# комментарий\nTG_TOKEN=123:abc\nMAX_PRICE=40000   # потолок\nTG_CHAT_ID=42\n"
    cfg = Config.load(str(env))
    assert (cfg.tg_token, cfg.tg_chat_id, cfg.max_price) == ("123:abc", "42", 40000)


def test_saved_search_notifies_only_about_new_matching(conn):
    conn.execute("INSERT INTO saved_searches(name, query, created, notify) VALUES('Дешёвые 3070', 'q=3070&pmax=21000', 0, 1)")
    out = []
    process(conn, Config(mode_urgent=False), [L(500, 20500), L(501, 26000)], "msk", False,
            notifier=Notifier(out=out.append), now=NOW + 60, category="videokarty", region="moskva")
    hits = [o for o in out if "Сохранённый поиск" in o]
    assert len(hits) == 1 and "Дешёвые 3070" in hits[0] and "20 500" in hits[0] and "26 000" not in hits[0]
    out.clear()
    process(conn, Config(), [L(500, 20500)], "msk", False, notifier=Notifier(out=out.append), now=NOW + 120,
            category="videokarty", region="moskva")
    assert not [o for o in out if "Сохранённый поиск" in o]                # уже не новое
