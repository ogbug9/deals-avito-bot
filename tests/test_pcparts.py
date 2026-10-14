"""Внутренняя стоимость ПК: разбор на детали, оценка каждой, фильтр и сортировка, аналитика по ушедшим."""
import json

import pytest

from gpuflip import db, pcparts
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.pipeline import process, rescore
from gpuflip.ui import App

NOW = 1_800_000_000.0
PC_TEXT = ("Процессор Intel Core i5-12400F, видеокарта RTX 3060 12GB, оперативная память 16 гб ddr4, "
           "ssd 512 гб nvme, блок питания 650w. Всё работает.")


def L(i, price, title, desc=""):
    return RawListing(id=str(i), title=title, price=price, url=f"https://www.avito.ru/x_{i}", description=desc)


def put(conn, items, category, t=NOW):
    return process(conn, Config(), items, f"s-{category}", False, now=t, category=category, region="moskva")


def fill_market(conn):
    put(conn, [L(i, 27000 + (i % 3) * 300, "Видеокарта RTX 3060 12GB") for i in range(12)], "videokarty")
    put(conn, [L(100 + i, 9000 + (i % 3) * 100, "Процессор Intel Core i5-12400F") for i in range(12)], "protsessory")
    put(conn, [L(200 + i, 3000 + (i % 3) * 100, "Оперативная память DDR4 16GB") for i in range(12)], "operativnaya_pamyat")


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    fill_market(c)
    return c


def parts_of(conn, i):
    r = conn.execute("SELECT parts_json, parts_value FROM listings WHERE id=?", (str(i),)).fetchone()
    return (json.loads(r["parts_json"]) if r["parts_json"] else None), r["parts_value"]


def test_each_named_part_is_priced_by_its_own_market(conn):
    out = pcparts.breakdown(conn, Config(), "Игровой компьютер", PC_TEXT, 30000, NOW + 60)
    by = {x["kind"]: x for x in out["items"]}
    assert [x["kind"] for x in out["items"]] == ["gpu", "cpu", "mb", "ram", "ssd", "psu", "case"]      # порядок показа
    assert by["gpu"]["status"] == by["cpu"]["status"] == by["ram"]["status"] == "ok"
    assert by["gpu"]["key"] == "RTX 3060 12GB" and by["gpu"]["value"] == round(27300 * 0.92) and by["gpu"]["n"] == 12
    assert by["ssd"]["status"] == "nomarket" and by["ssd"]["value"] is None       # позицию узнали, цену не выдумываем
    assert by["psu"]["status"] == "nomarket" and by["mb"]["status"] == by["case"]["status"] == "unknown"
    assert out["value"] == by["gpu"]["value"] + by["cpu"]["value"] + by["ram"]["value"]
    assert out["priced"] == 3 and out["expected"] == 6
    assert out["missing"] == ["материнская плата", "накопитель", "блок питания"]
    assert out["diff"] == out["value"] - 30000 and out["diff"] > 0
    assert out["verdict"].startswith("оценённые детали дороже лота на ") and "не оценено: материнская плата, накопитель" in out["verdict"]
    # продажа по частям: минус торг уже учтён в покупке, минус издержки на каждую деталь
    cfg = Config()
    assert out["net"] == round(out["value"] - 27000 - cfg.fixed_costs - 150 * 3 - 27000 * cfg.capital_cost_per_day * cfg.bundle_days)
    assert not out["warn"]


def test_expensive_pc_is_covered_only_partly(conn):
    out = pcparts.breakdown(conn, Config(), "Игровой компьютер", PC_TEXT, 60000, NOW + 60)
    assert out["diff"] < 0 and 0 < out["ratio"] < 1 and "покрывают" in out["verdict"] and out["net"] < 0


def test_family_price_per_gb_is_marked_approximate(conn):
    put(conn, [L(300 + i, 4000 + (i % 4) * 200, "SSD M.2 NVMe 1TB Samsung") for i in range(12)], "zhestkie_diski")
    out = pcparts.breakdown(conn, Config(), "Игровой компьютер", PC_TEXT, 30000, NOW + 60)
    ssd = next(x for x in out["items"] if x["kind"] == "ssd")
    assert ssd["status"] == "approx" and ssd["value"] and "похожих" in ssd["note"] and out["approx"]
    assert ssd["value"] < 4600 * 0.6            # 512 ГБ стоит заметно меньше терабайта
    assert "накопитель" not in out["missing"]


def test_memory_type_is_taken_from_the_platform_when_not_written(conn):
    from gpuflip import parts
    assert parts.platform_ddr("игровой пк ryzen5 3600/rx580 8gb/16gbram/500gbnvme") == 4
    assert parts.platform_ddr("ryzen 7 7800x3d rtx 5070 ti 32 гб озу") == 5
    assert parts.platform_ddr("компьютер i5-3470 8 гб озу") == 3
    assert parts.platform_ddr("i5-12400f 16 гб озу") is None                 # 12-е поколение: бывает и DDR4, и DDR5
    assert parts.platform_ddr("плата b450m, 16 гб озу") == 4
    out = pcparts.breakdown(conn, Config(), "Игровой пк Ryzen5 3600/RX580 8GB/16gbram/500gbnvme", "", 36000, NOW + 60)
    ram = next(x for x in out["items"] if x["kind"] == "ram")
    assert ram["key"] == "RAM DDR4 16GB" and ram["status"] == "ok" and "по платформе" in ram["note"]
    video = pcparts.breakdown(conn, Config(), "Игровой пк", "Ryzen 5 3600, RTX 3060 12GB, видеопамять 8 гб, vram 8gb", 30000, NOW + 60)
    assert next(x for x in video["items"] if x["kind"] == "ram")["status"] == "unknown"     # видеопамять — не ОЗУ
    plain = pcparts.breakdown(conn, Config(), "Игровой компьютер", PC_TEXT, 30000, NOW + 60)
    assert "по платформе" not in next(x for x in plain["items"] if x["kind"] == "ram")["note"]


def test_absent_and_unknown_parts(conn):
    out = pcparts.breakdown(conn, Config(), "Системный блок без видеокарты", "Процессор i5-12400F, 16 гб ddr4", 9000, NOW + 60)
    by = {x["kind"]: x for x in out["items"]}
    assert by["gpu"]["status"] == "absent" and out["expected"] == 5 and "видеокарта" not in out["missing"]
    empty = pcparts.breakdown(conn, Config(), "Компьютер для офиса", "Работает, отдам с монитором", 5000, NOW + 60)
    assert empty["value"] == 0 and empty["diff"] is None and empty["net"] is None and "не оценены" in empty["verdict"]
    assert pcparts.breakdown(conn, Config(), "Ноутбук Asus RTX 3060", "", 50000, NOW + 60) is None


def test_single_part_dearer_than_the_whole_pc_is_a_warning(conn):
    out = pcparts.breakdown(conn, Config(), "Игровой пк RTX 3060 12GB", "", 15000, NOW + 60)
    assert out["value"] > 15000 and "обман" in out["warn"]


def test_breakdown_is_stored_filtered_and_sorted(tmp_path):
    path = str(tmp_path / "pc.db")
    conn = db.connect(path)
    fill_market(conn)
    put(conn, [L(900, 30000, "Игровой компьютер", PC_TEXT), L(901, 60000, "Игровой пк", PC_TEXT),
               L(902, 5000, "Компьютер для офиса", "Работает"), L(903, 50000, "Ноутбук игровой RTX 3060 12GB")], "pc", NOW + 60)
    p900, v900 = parts_of(conn, 900)
    assert v900 == p900["value"] > 30000
    assert parts_of(conn, 902)[1] is None and parts_of(conn, 902)[0]["items"]           # разбор есть, оценки нет
    assert parts_of(conn, 903) == (None, None)                                           # ноутбук не разбираем
    assert parts_of(conn, 5) == (None, None)                                             # видеокарта — не ПК
    conn.commit()
    conn.close()
    app = App(path)
    q = lambda **kw: {k: [str(v)] for k, v in {"status": "all", **kw}.items()}
    assert [i["id"] for i in app.listings(q(pcgain=1))["items"]] == ["900"]
    order = [i["id"] for i in app.listings(q(cat="pc", sort="parts"))["items"]]
    assert order[:2] == ["900", "901"] and set(order[2:]) == {"902", "903"}             # без оценки — в конце
    assert [i["id"] for i in app.listings(q(cat="pc", sort="parts", dir="flip"))["items"]][:2] == ["901", "900"]
    row = next(i for i in app.listings(q(cat="pc"))["items"] if i["id"] == "900")
    assert row["parts"]["priced"] == 3 and row["parts_value"] == v900 and "parts_json" not in row
    assert app.listing("900")["parts"]["items"][0]["label"] == "Видеокарта"
    csv_text = app.export_csv(q(cat="pc", sort="parts"))
    head, first = csv_text.splitlines()[:2]
    cols = head.split(";")
    assert "ПК: детали, ₽" in cols and first.split(";")[cols.index("ПК: оценено деталей")] == "3 из 6"


def test_rescore_covers_gone_listings_too(conn):
    put(conn, [L(900, 30000, "Игровой компьютер", PC_TEXT)], "pc", NOW + 60)
    put(conn, [L(950, 26000, "Видеокарта RTX 3060 12GB")], "videokarty", NOW + 60)
    conn.execute("UPDATE listings SET status='inactive', gone_at=?, parts_json=NULL, parts_value=NULL, score_json=NULL, "
                 "cond=NULL, item_key=NULL WHERE id IN ('900','950')", (NOW + 100,))
    res = rescore(conn, Config(), now=NOW + 200)
    assert res["gone"] == 2
    pc = conn.execute("SELECT status, gone_at, last_seen, parts_value, cond FROM listings WHERE id='900'").fetchone()
    assert pc["status"] == "inactive" and pc["gone_at"] == NOW + 100 and pc["last_seen"] == NOW + 60
    assert pc["parts_value"] > 30000 and pc["cond"] == "used"
    card = conn.execute("SELECT status, item_key, score_json, mode_tags FROM listings WHERE id='950'").fetchone()
    assert card["status"] == "inactive" and card["item_key"] == "RTX 3060 12GB" and json.loads(card["score_json"])["quick_price"]
    assert db.market_stats(conn, "RTX 3060 12GB", None, NOW + 200).n == 12              # ушедший лот рынок не двигает


def test_cpu_without_own_market_takes_a_near_twin(conn):
    assert pcparts.cpu_siblings("CPU i5-12400") == ["CPU i5-12400F", "CPU i5-12400K", "CPU i5-12400KF"]
    assert "CPU Ryzen 5 5600" in pcparts.cpu_siblings("CPU Ryzen 5 5600X")
    assert pcparts.cpu_siblings("CPU Ryzen 7 7800X3D") == [] and pcparts.cpu_siblings("CPU Ryzen 5 5600G") == []
    out = pcparts.breakdown(conn, Config(), "Игровой пк", "Процессор Intel Core i5-12400, видеокарта RTX 3060 12GB", 30000, NOW + 60)
    cpu = next(x for x in out["items"] if x["kind"] == "cpu")
    twin = round(round(9100 * 0.92) * 0.95)
    assert cpu["key"] == "CPU i5-12400" and cpu["status"] == "approx" and cpu["value"] == pytest.approx(twin, abs=2)
    assert "i5-12400F" in cpu["note"] and out["approx"]


def test_ladder_shows_neighbours_with_prices(conn):
    from gpuflip import analogs
    put(conn, [L(400 + i, 32000 + (i % 3) * 300, "Видеокарта RTX 3070 8GB") for i in range(12)]
        + [L(430 + i, 36000, "Видеокарта RTX 3070 Ti 8GB") for i in range(8)]
        + [L(460, 9000, "Видеокарта RTX 3070 Ti 8GB")], "videokarty")              # приманка вдвое дешевле рынка
    lad = analogs.ladder(conn, "RTX 3070", NOW + 60)
    keys = [x["key"] for x in lad["items"]]
    assert lad["kind"] == "index" and keys.index("RTX 3060 12GB") < keys.index("RTX 3070") < keys.index("RTX 3070 Ti")
    ti = next(x for x in lad["items"] if x["key"] == "RTX 3070 Ti")
    assert ti["diff"] == 36000 - 32300 and ti["cheapest"]["price"] == 36000            # приманку за 9 000 не советуем
    assert next(x for x in lad["items"] if x["current"])["key"] == "RTX 3070"
    ram = analogs.ladder(conn, "RAM DDR4 16GB", NOW + 60)
    assert ram is None or ram["kind"] == "price"
    assert analogs.ladder_pattern("CPU i5-12400F") == ("CPU i_-12%", "Intel Core 12-го поколения")
    assert analogs.ladder_pattern("CPU Ryzen 5 5600X")[0] == "CPU Ryzen _ 5%"
    assert analogs.ladder_pattern("MB AM4 B550 mATX (средняя)")[0] == "MB AM4 %"


def test_pc_is_compared_with_other_pcs_with_the_same_card(conn):
    pcs = [L(1000 + i, 40000 + i * 500, "Игровой пк RTX 3060 12GB", "Ryzen 5 5600, 16 гб ddr4") for i in range(8)]
    put(conn, pcs, "pc", NOW + 60)
    put(conn, pcs + [L(1100, 30000, "Игровой пк RTX 3060 12GB i5-12400F")], "pc", NOW + 120)
    p, _ = parts_of(conn, 1100)
    import statistics
    med = statistics.median(40000 + i * 500 for i in range(8))
    assert p["peers"]["key"] == "RTX 3060 12GB" and p["peers"]["n"] == 8 and p["peers"]["median"] == med
    assert p["peers"]["diff_pct"] == pytest.approx((med - 30000) / med)
    lone, _ = parts_of(conn, 1000)
    assert lone["peers"]["n"] >= 7          # новый лот попадёт в сравнение со следующего обхода (пересчёт идёт в два прохода)
