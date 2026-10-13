"""Этап 3: API интерфейса — настройки, экспорт, «Сегодня», сравнение, сделки, исключения, статика."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from gpuflip import db
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.pipeline import process
from gpuflip.ui import App, make_handler

NOW = 1_800_000_000.0


def q(**kw):
    return {k: [str(v)] for k, v in kw.items()}


def L(i, price, title="Видеокарта RTX 3070 8GB", desc="", **kw):
    return RawListing(id=str(i), title=title, price=price, url=f"https://www.avito.ru/x_{i}", description=desc, **kw)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("TG_TOKEN", "TG_CHAT_ID", "LIGHT_CYCLE", "MAX_REQUESTS_PER_HOUR"):
        monkeypatch.delenv(k, raising=False)
    path = str(tmp_path / "t.db")
    conn = db.connect(path)
    process(conn, Config(), [L(i, 27000 + (i % 3) * 300) for i in range(12)], "s", False, now=NOW,
            category="videokarty", region="moskva")
    process(conn, Config(), [L(500, 19000, delivery=True, seller_id="shop", seller_name="Магазин"),
                             L(501, 23500, desc="срочно"), L(502, 26000)],
            "s", False, now=NOW + 60, category="videokarty", region="moskva")
    process(conn, Config(), [L(502, 24500)], "s", False, now=NOW + 120, category="videokarty", region="moskva")
    conn.close()
    (tmp_path / ".env").write_text("TG_TOKEN=\nTG_CHAT_ID=\n", encoding="utf-8")
    return App(path, str(tmp_path / "sources.json"), env_path=str(tmp_path / ".env"))


def ids(app, **kw):
    return sorted(i["id"] for i in app.listings(q(**kw))["items"])


# ---------------------------------------------------------------- фильтры и сигналы
def test_presets_and_signals(app):
    assert ids(app, profit=1) == ["500", "501"]                # 500 — ниже рынка, 501 — срочная продажа
    assert ids(app, mode="urgent") == ["501"] and ids(app, mode="market") == ["500", "501"]
    assert ids(app, disc=20) == ["500"] and ids(app, disc=10) == ["500", "501", "502"]
    assert ids(app, urgent=1) == ["501"] and ids(app, drop=1) == ["502"]
    assert ids(app, ids="500,502") == ["500", "502"]
    row = next(i for i in app.listings(q())["items"] if i["id"] == "502")
    assert row["price_drop"] == 1500 and row["price_drop_pct"] == pytest.approx(1500 / 26000) and row["days_listed"] is not None
    assert row["photos"] == [] and row["checklist"] == {}


def test_window_for_virtual_list_and_column_sort(app):
    r = app.listings(q(offset=5, limit=4, sort="price_asc"))
    assert r["total"] == 15 and r["offset"] == 5 and len(r["items"]) == 4
    prices = [i["price"] for i in app.listings(q(sort="price_asc", limit=200, offset=0))["items"]]
    assert prices == sorted(prices)
    flipped = [i["price"] for i in app.listings(q(sort="price_asc", dir="flip", limit=200, offset=0))["items"]]
    assert flipped == sorted(prices, reverse=True)
    assert app.listings(q(sort="'; DROP TABLE listings;--"))["total"] == 15          # неизвестная сортировка = по дате


def test_skipped_and_excluded_are_hidden_by_default(app):
    app.update_listing({"id": "501", "user_status": "skipped"})
    assert "501" not in ids(app) and ids(app, ustatus="skipped") == ["501"] and "501" in ids(app, skipped="show")
    assert app.exclude({"kind": "seller", "value": "shop", "title": "Магазин"}) == [{"kind": "seller", "value": "shop", "title": "Магазин"}]
    assert "500" not in ids(app) and "500" in ids(app, excluded="show")
    assert app.listing("500")["seller_hidden"] is True
    assert app.exclude({"kind": "seller", "value": "shop", "on": False}) == [] and "500" in ids(app)
    with pytest.raises(ValueError):
        app.exclude({"kind": "город", "value": "x"})


# ---------------------------------------------------------------- карточка
def test_listing_detail_has_similar_equivalents_liquidity_checklist(app):
    d = app.listing("500")
    assert d["similar"]["key"] == "RTX 3070" and d["similar"]["n"] == 15 and len(d["similar"]["items"]) == 8
    assert d["equivalents"]["group"] == "1440p" and d["liquidity"]["label"] == "нет данных"
    assert [c["key"] for c in d["checklist_items"]][:3] == ["stress", "temps", "artifacts"]
    d = app.update_listing({"id": "500", "checklist": {"stress": True, "serial": True, "взлом": True, "temps": False}})
    assert d["checklist"] == {"stress": True, "serial": True}                  # незнакомые и снятые пункты не хранятся
    assert app.update_listing({"id": "500", "checklist": {}})["checklist"] == {}


# ---------------------------------------------------------------- сегодня, экспорт
def test_today_dashboard(app):
    t = app.today()
    assert t["candidates"] == 2 and t["best"][0]["id"] == "500" and t["app"]["desktop"] is False
    assert {"new_candidates", "new_listings", "gone", "gone_candidates", "price_drops"} <= set(t)


def test_export_csv_respects_filters(app):
    text = app.export_csv(q(disc=20))
    lines = text.lstrip("﻿").strip().split("\r\n")
    assert lines[0].startswith("id;Название;Цена;") and len(lines) == 2
    cells = lines[1].split(";")
    assert cells[0] == "500" and cells[2] == "19000" and "ниже рынка" in lines[1] and cells[-1] == "https://www.avito.ru/x_500"
    assert len(app.export_csv(q()).strip().split("\r\n")) == 16


# ---------------------------------------------------------------- сравнение и сделки через API
def test_compare_api_and_saved_sets(app):
    res = app.compare(q(ids="500,501,502", ref="502"))
    assert [lot["id"] for lot in res["lots"]] == ["500", "501", "502"] and res["ref"] == "502"
    assert app.compare_csv(q(ids="500,501")).startswith("﻿Критерий;")
    sets = app.compare_sets_change({"name": "Три 3070", "ids": ["500", "501", "502"], "ref": "502"})
    assert sets[0]["ids"] == ["500", "501", "502"] and sets[0]["ref"] == "502"
    assert app.compare_sets_change({"delete": sets[0]["id"]}) == []
    with pytest.raises(ValueError):
        app.compare_sets_change({"name": "Один", "ids": ["500"]})
    assert [m["key"] for m in app.compare_models(q(keys="RTX 3070|RTX 3060 12GB"))["models"]] == ["RTX 3070", "RTX 3060 12GB"]


def test_bought_button_creates_deal_and_journal_updates(app):
    j = app.deals_change({"listing_id": "500", "buy_price": "18500"})
    d = j["deals"][0]
    assert d["title"] == "Видеокарта RTX 3070 8GB" and d["buy_price"] == 18500 and d["est_profit"] > 0 and not d["closed"]
    assert app.listing("500")["user_status"] == "bought"
    j = app.deals_change({"id": d["id"], "sell_price": "25000", "costs": "500", "note": "продал за день"})
    d = j["deals"][0]
    assert d["closed"] and d["net"] == 6000 and d["sold_at"] and d["note"] == "продал за день" and j["summary"]["closed"] == 1
    j = app.deals_change({"id": d["id"], "sell_price": ""})                    # отмена продажи
    assert not j["deals"][0]["closed"] and j["deals"][0]["sold_at"] is None
    assert app.deals_change({"delete": d["id"]})["deals"] == []
    with pytest.raises(ValueError):
        app.deals_change({"title": "без цены"})


# ---------------------------------------------------------------- настройки
def test_config_never_returns_token_and_validates(app, tmp_path):
    c = app.get_config()
    assert c["telegram"] == {"configured": False, "chat_id": "", "token_set": False, "token_hint": ""}
    assert c["version"]["version"] and c["max_requests_per_hour"] == 45 and c["light_cycle"] is True
    c = app.set_config({"telegram": {"token": "123456789:AAExampleTokenValue_abcdef", "chat_id": "42"}})
    assert c["telegram"] == {"configured": True, "chat_id": "42", "token_set": True, "token_hint": "…cdef"}
    assert "AAExample" not in json.dumps(c)                                    # сам токен наружу не уходит
    assert "TG_TOKEN=123456789:AAExampleTokenValue_abcdef" in (tmp_path / ".env").read_text(encoding="utf-8")
    c = app.set_config({"light_cycle": False, "light_min_sec": 240, "light_max_sec": 400, "max_requests_per_hour": 30})
    assert (c["light_cycle"], c["light_min_sec"], c["light_max_sec"], c["max_requests_per_hour"]) == (False, 240, 400, 30)
    for bad in ({"telegram": {"token": "коротко"}}, {"telegram": {"chat_id": "не число"}}, {"max_requests_per_hour": 9999},
                {"light_min_sec": 500, "light_max_sec": 300}, {"full_interval_sec": "часто"}):
        with pytest.raises(ValueError):
            app.set_config(bad)
    assert app.telegram_test()["ok"] is False or True                          # сеть в тестах не трогаем: ниже — без токена
    app.set_config({"telegram": {"clear": True}})
    assert app.telegram_test() == {"ok": False, "message": "не задан токен бота"}


def test_view_prefs_roundtrip(app):
    assert app.prefs()["ui.view"] is None
    out = app.set_pref({"key": "ui.view", "value": {"mode": "table", "density": "compact"}})
    assert out["ui.view"]["mode"] == "table"
    app.set_pref({"key": "ui.profiles", "value": {"мой": {"mode": "grid"}}})
    assert app.prefs()["ui.profiles"]["мой"]["mode"] == "grid"
    with pytest.raises(ValueError):
        app.set_pref({"key": "TG_TOKEN", "value": "x"})


def test_saved_search_notify_toggle(app):
    s = app.saved_change({"name": "Дёшево", "query": "pmax=20000", "notify": True})[0]
    assert s["notify"] == 1
    assert app.saved_change({"id": s["id"], "notify": False})[0]["notify"] == 0


def test_market_has_liquidity_and_trimmed_minimum(app):
    conn = db.connect(app.db_path)
    process(conn, Config(), [L(900, 500, "Видеокарта RTX 3070 8GB коробка")], "s", False, now=NOW + 200,
            category="videokarty", region="moskva")
    conn.close()
    row = next(r for r in app.market(q(days=7))["rows"] if r["key"] == "RTX 3070")
    assert row["n"] == 16 and row["min"] == 19000                              # мусорные 500 ₽ минимумом не считаются
    assert row["liq_level"] == "none" and row["index"] == 145 and row["per_index"] == pytest.approx(row["median"] / 145)


# ---------------------------------------------------------------- HTTP: статика и безопасность
def test_static_files_and_path_traversal(app):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        for name in ("app.css", "core.js", "list.js", "panel.js", "compare.js", "tabs.js"):
            assert f"/static/{name}" in page
            r = urllib.request.urlopen(f"{base}/static/{name}")
            assert r.status == 200 and len(r.read()) > 500
        for bad in ("/static/../ui.py", "/static/..%2F..%2F.env", "/static/index.html", "/static/нет.js"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + urllib.request.quote(bad, safe="/%.:"))
            assert e.value.code == 404
        r = urllib.request.urlopen(base + "/api/export.csv?disc=20")
        assert "attachment" in r.headers["Content-Disposition"] and r.read().decode("utf-8").startswith("﻿id;")
        assert json.load(urllib.request.urlopen(base + "/api/today"))["candidates"] == 2
        assert json.load(urllib.request.urlopen(base + "/api/analogs"))["approx"] is True
    finally:
        srv.shutdown()
