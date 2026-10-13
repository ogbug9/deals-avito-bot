"""Блоки C и D: сравнение лотов и моделей, аналоги."""
import json

import pytest

from gpuflip import analogs, compare, db
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.pipeline import process

NOW = 1_800_000_000.0
DAY = 86400.0


def L(i, price, title="Видеокарта RTX 3070 8GB", desc="", **kw):
    return RawListing(id=str(i), title=title, price=price, url=f"https://www.avito.ru/x_{i}", description=desc, **kw)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    base = [L(i, 27000 + (i % 3) * 300) for i in range(12)]
    process(c, Config(), base, "msk", False, now=NOW, category="videokarty", region="moskva")
    lots = [L(500, 19000, delivery=True, rating=4.9, date_text="2 часа назад"),
            L(501, 24000, desc="после майнинга, не тестировалась", rating=4.2),
            L(502, 27000, delivery=True)]
    process(c, Config(), lots, "msk", False, now=NOW + 60, category="videokarty", region="moskva")
    return c


def row(res, key):
    return next(r for r in res["rows"] if r["key"] == key)


def marks(res, key):
    return [c["mark"] for c in row(res, key)["cells"]]


# ---------------------------------------------------------------- сравнение лотов
def test_best_and_worst_are_marked_by_direction(conn):
    res = compare.build(conn, ["500", "501", "502"], now=NOW + 120)
    assert [lot["id"] for lot in res["lots"]] == ["500", "501", "502"]
    assert marks(res, "price") == ["best", "", "worst"]                 # меньше — лучше
    assert marks(res, "net") == ["best", "", "worst"]                   # больше — лучше
    assert marks(res, "est_profit") == ["", "", ""]                     # значение есть только у одного — сравнивать не с чем
    assert marks(res, "rating") == ["best", "worst", ""]                # у третьего рейтинга нет — он не участвует
    assert marks(res, "risk") == ["best", "worst", "best"]              # два лота без риска — оба лучшие
    assert marks(res, "delivery") == ["best", "worst", "best"]
    assert marks(res, "model") == ["", "", ""]                          # несравнимый критерий
    assert [c["text"] for c in row(res, "price")["cells"]] == ["19 000 ₽", "24 000 ₽", "27 000 ₽"]
    assert row(res, "vs_median")["cells"][0]["text"] == "-30%"
    assert "майнинг" in row(res, "risk_words")["cells"][1]["text"]


def test_equal_values_get_no_marks(conn):
    res = compare.build(conn, ["0", "3"], now=NOW + 120)                # два одинаковых лота из рынка
    assert marks(res, "price") == ["", ""] and marks(res, "n") == ["", ""]


def test_only_differences(conn):
    res = compare.build(conn, ["500", "502"], now=NOW + 120)
    keys = {r["key"] for r in compare.only_differences(res)}
    assert "price" in keys and "est_profit" in keys
    assert not {"model", "city", "delivery", "source", "condition"} & keys      # одинаковое скрыто
    assert row(res, "model")["differs"] is False


def test_reference_column_and_diff(conn):
    res = compare.build(conn, ["500", "501", "502"], ref="502", now=NOW + 120)
    assert res["ref"] == "502"
    price = row(res, "price")["cells"]
    assert [c["diff"] for c in price] == ["-8 000 ₽", "-3 000 ₽", ""]
    assert row(res, "vs_median")["cells"][0]["diff"].endswith("п.п.")
    assert compare.build(conn, ["500", "501"], ref="нет", now=NOW)["ref"] is None


def test_limit_of_six_lots_and_unknown_ids(conn):
    res = compare.build(conn, [str(i) for i in range(10)] + ["нет"], now=NOW)
    assert len(res["lots"]) == compare.MAX_LOTS
    assert compare.build(conn, [], now=NOW)["lots"] == []


def test_csv_export(conn):
    res = compare.build(conn, ["500", "501"], ref="500", now=NOW + 120)
    text = compare.to_csv(res)
    lines = text.lstrip("﻿").split("\r\n")
    assert lines[0] == "Критерий;Видеокарта RTX 3070 8GB;Видеокарта RTX 3070 8GB"
    assert lines[1].startswith("Ссылка;https://www.avito.ru/x_500")
    assert "Цена;19 000 ₽;24 000 ₽ (+5 000 ₽)" in lines
    assert not any(line.startswith("Фото") for line in lines)
    assert len(compare.to_csv(res, only_diff=True)) < len(text)


# ---------------------------------------------------------------- сравнение моделей
def test_models_comparison_uses_market_and_analog_index(conn):
    process(conn, Config(), [L(600 + i, 29000 + (i % 3) * 300, "Видеокарта RTX 3060 12GB") for i in range(12)],
            "msk", False, now=NOW, category="videokarty", region="moskva")
    res = compare.models(conn, ["RTX 3070", "RTX 3060 12GB", "GTX 750"], now=NOW + 120)
    assert [m["key"] for m in res["models"]] == ["RTX 3070", "RTX 3060 12GB", "GTX 750"] and res["approx"] is True
    idx = row(res, "index")["cells"]
    assert [c["text"] for c in idx] == ["145", "100", "нет данных"]             # модели нет в таблице — индекс не выдумываем
    per = row(res, "per_index")["cells"]
    assert per[0]["mark"] == "best" and per[1]["mark"] == "worst" and per[2]["text"] == "—"
    assert row(res, "median")["cells"][0]["mark"] == "best"
    assert row(res, "liquidity")["cells"][0]["text"] == "—"
    assert len(compare.models(conn, list("abcdefg"), now=NOW)["models"]) == 4


# ---------------------------------------------------------------- аналоги
def test_analog_table_is_marked_approximate_and_has_no_invented_gaps():
    t = analogs.load()
    assert t["approx"] is True and t["error"] == "" and len(t["groups"]) >= 6
    assert all("приблизительно" in g["note"] for g in t["groups"])
    assert analogs.index_of("RTX 3060 12GB") == 100 and analogs.index_of("RTX 5090") == 480
    assert analogs.index_of("Несуществующая") is None and analogs.group_of("RTX 3070")["name"] == "1440p"


def test_cheaper_equivalents_from_same_group(conn):
    process(conn, Config(), [L(700 + i, 22000 + (i % 3) * 200, "Видеокарта RTX 3060 Ti 8GB") for i in range(8)]
            + [L(720 + i, 40000, "Видеокарта RTX 3070 Ti 8GB") for i in range(8)]
            + [L(740 + i, 15000, "Видеокарта RX 6700 XT 12GB") for i in range(3)],        # мало объявлений — не показываем
            "msk", False, now=NOW, category="videokarty", region="moskva")
    eq = analogs.cheaper_equivalents(conn, "RTX 3070", NOW + 60)
    assert eq["group"] == "1440p" and eq["approx"] is True
    assert [i["key"] for i in eq["items"]] == ["RTX 3060 Ti"]
    it = eq["items"][0]
    assert it["saving"] > 4000 and it["index"] == 128 and it["index_diff"] == pytest.approx(128 / 145 - 1)
    assert analogs.cheaper_equivalents(conn, "Неизвестная модель", NOW)["items"] == []


def test_user_can_edit_analogs_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GPUFLIP_HOME", str(tmp_path))
    (tmp_path / "words").mkdir()
    (tmp_path / "words" / "analogs.json").write_text(
        json.dumps({"approx": False, "groups": [{"name": "Мои", "note": "замерил сам", "models": {"RTX 3070": 150}}]}),
        encoding="utf-8")
    assert analogs.index_of("RTX 3070") == 150 and analogs.load()["approx"] is False
    (tmp_path / "words" / "analogs.json").write_text("{битый json", encoding="utf-8")
    t = analogs.load()
    assert t["groups"] == [] and "не удалось прочитать" in t["error"]


def test_similar_lots_and_price_spread(conn):
    s = analogs.similar(conn, "500", NOW + 120, limit=4)
    assert s["key"] == "RTX 3070" and s["n"] == 15 and len(s["items"]) == 4
    assert s["prices"] == sorted(s["prices"]) and s["prices"][0] == 19000 and s["median"] == 27300
    assert s["items"][0]["price"] == 24000 and s["items"][0]["label"] == "дешевле рынка" and s["items"][0]["vs_this"] == 5000
    assert s["items"][-1]["label"] == "по рынку"
    assert s["cheaper_than_share"] == 1.0                                # дешевле всех в позиции
    assert analogs.similar(conn, "нет", NOW)["items"] == []
