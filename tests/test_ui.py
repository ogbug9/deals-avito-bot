import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from gpuflip import db, sources
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.pipeline import process
from gpuflip.ui import App, make_handler


def q(**kw):
    return {k: [str(v)] for k, v in kw.items()}


@pytest.fixture
def app(tmp_path):
    path = str(tmp_path / "t.db")
    conn = db.connect(path)
    cfg = Config()
    gpus = [RawListing("1", "Видеокарта RTX 3060 12GB", 28000, "https://www.avito.ru/a_1", "Отличная, без артефактов",
                       "ВДНХ, до 5 мин.", delivery=True, date_text="2 часа назад", rating=4.9, reviews=10),
            RawListing("2", "Видеокарта GTX 1660 Super", 9000, "https://www.avito.ru/a_2", "кулера не крутятся", "Перерва"),
            RawListing("3", "Куплю видеокарту RTX 3060", 20000, "https://www.avito.ru/a_3")]
    process(conn, cfg, gpus, "s1", False, now=1000.0, category="videokarty", region="moskva")
    cpus = [RawListing("4", "Процессор Ryzen 5 5600", 9500, "https://www.avito.ru/a_4", "Рабочий", "Казань", delivery=True),
            RawListing("5", "Процессор i5-12400F", 11000, "https://www.avito.ru/a_5", "", "Казань")]
    process(conn, cfg, cpus, "s2", False, now=1000.0, category="protsessory", region="all")
    conn.close()
    return App(path, str(tmp_path / "sources.json"))


def ids(app, **kw):
    return sorted(i["id"] for i in app.listings(q(**kw))["items"])


def test_default_hides_defective_and_wanted(app):
    assert ids(app) == ["1", "4", "5"]
    assert ids(app, defect="show", wanted="show") == ["1", "2", "3", "4", "5"]


def test_filters(app):
    assert ids(app, cat="protsessory") == ["4", "5"]
    assert ids(app, pmin=9000, pmax=10000) == ["4"]
    assert ids(app, delivery=1) == ["1", "4"]
    assert ids(app, region="moskva") == ["1"]
    assert ids(app, city="казань") == ["4", "5"]          # регистр кириллицы
    assert ids(app, q="RYZEN 5600") == ["4"]
    assert ids(app, model="RTX 3060 12GB") == ["1"]
    assert ids(app, rating="4.8") == ["1"]
    assert ids(app, cat="videokarty,'; DROP TABLE listings;--") == ["1"]   # мусор в категории отбрасывается


def test_sort_and_paging(app):
    r = app.listings(q(sort="price_asc", per=2))
    assert [i["id"] for i in r["items"]] == ["4", "5"] and r["total"] == 3
    assert [i["id"] for i in app.listings(q(sort="price_asc", per=2, page=2))["items"]] == ["1"]


def test_facets(app):
    f = app.facets()
    assert f["total"] == 5 and f["clean"] == 3
    assert {c["key"]: c["n"] for c in f["categories"]} == {"videokarty": 3, "protsessory": 2}


def test_non_gpu_categories_are_not_scored(app):
    row = app.listings(q(cat="protsessory"))["items"][0]
    assert row["gpu_key"] is None and row["category_title"] == "Процессоры"
    assert row["score"] is None or not row["score"]["ok"]      # позиция есть, но рынка для оценки нет


def test_sources_roundtrip_and_validation(app):
    d = app.get_sources()
    assert not d["saved"] and len(d["sources"]) == len(sources.DEFAULT_SOURCES)
    out = app.set_sources([{"name": "", "category": "videokarty", "region": "all", "delivery_only": True,
                            "pmin": 5000, "pmax": 40000, "query": "rtx 3060", "max_pages": 99, "enabled": True}])
    s = out["sources"][0]
    assert out["saved"] and s["max_pages"] == 20 and s["name"]
    assert s["url"] == ("https://www.avito.ru/all/tovary_dlya_kompyutera/komplektuyuschie/"
                        "videokarty-ASgBAgICAkTGB~pm7gmmZw?d=1&pmin=5000&pmax=40000&q=rtx+3060&s=104")
    with pytest.raises(ValueError):
        app.set_sources([{"name": "x", "category": "nope"}])
    with pytest.raises(ValueError):
        app.set_sources([{"name": "x", "category": "videokarty", "region": "moskva/../evil"}])


def test_http_rejects_foreign_origin(app):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        assert json.load(urllib.request.urlopen(base + "/api/facets"))["total"] == 5
        assert b"gpuflip" in urllib.request.urlopen(base + "/").read()
        req = urllib.request.Request(base + "/api/sources", data=b"[]", method="POST",
                                     headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    finally:
        srv.shutdown()
