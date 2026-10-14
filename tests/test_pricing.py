"""Цена быстрой продажи, потолок по более сильным моделям, мусор, «новое / б/у»."""
import json

import pytest

from gpuflip import analogs, db
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.normalizer import is_new, junk_reason
from gpuflip.pipeline import process
from gpuflip.scoring import price_points, score_listing

NOW = 1_800_000_000.0
DAY = 86400.0


def L(i, price, title="Видеокарта RTX 3070 8GB", desc="", **kw):
    return RawListing(id=str(i), title=title, price=price, url=f"https://www.avito.ru/x_{i}", description=desc, **kw)


def put(conn, items, t=NOW):
    return process(conn, Config(), items, "s", False, now=t, category="videokarty", region="moskva")


def score(conn, i):
    return json.loads(conn.execute("SELECT score_json FROM listings WHERE id=?", (str(i),)).fetchone()[0])


# ---------------------------------------------------------------- мусор
@pytest.mark.parametrize("title,desc,reason", [
    ("Коробка от RTX pro 6000", "Коробка от Rtx pro 6000. В наличии 8 шт.", "box"),
    ("Коробка от видеокарты 4060 Palit", "Пустая коробка, внутри ложемент", "box"),
    ("Gtx 1060 6gb msi armor", "Полностью рабочая. Охлаждение отсутствует, сломалось.", "incomplete"),
    ("Asus GTX 1070 ROG Strix 8gb", "Снята штатная система охлаждения", "incomplete"),
    ("Системный блок", "пустой корпус, без начинки", "incomplete"),
    ("Охлаждение от RTX 3090 ventus", "", "incomplete"),
    ("Rx 580 8gb", "Продам под восстание или на донора. Код ошибки 43", "scrap"),
    ("Видеокарты", "Платы видеокарт на доноры или восстановление.", "scrap"),
    ("Видеокарта GTX 760", "в утиль, на разбор", "scrap"),
    ("Мосты NVLink для RTX 2080 Ti под заказ из Китая", "", "accessory"),
    ("Заглушка Asrock b450m-hdv", "Задняя планка. Цена за 1 шт", "accessory"),
    ("Комплект полный от RTX 5080 Asus Prime", "", "box"),
])
def test_junk_is_recognised(title, desc, reason):
    assert junk_reason(title, desc) == reason


@pytest.mark.parametrize("title,desc,category", [
    ("Процессор Intel Core i5-7640X", "Box (без кулера)", "protsessory"),
    ("Корпус для пк ATX чёрный без комплектующих", "", "korpusa"),
    ("Кулер для видеокарты asus", "Остался лишний", "sistemy_ohlazhdeniya"),
    ("Вентиляторы для пк 120мм", "", "sistemy_ohlazhdeniya"),
    ("Amd radeon rx 580 8gb", "Рабочая. Так же можно разобрать на детали", "videokarty"),
    ("Компьютер", "Компьютер, всё работает. Но можно на запчасти.", "pc"),
])
def test_normal_goods_of_their_own_section_are_not_junk(title, desc, category):
    assert junk_reason(title, desc, category) == ""


def test_accessories_are_normal_goods_in_their_own_section():
    assert junk_reason("Кабель питания видеокарты 8 pin", "", "komplektuyuschie") == ""
    assert junk_reason("Райзер для видеокарты", "", "komplektuyuschie") == ""
    assert junk_reason("Кабель питания видеокарты 8 pin", "", "videokarty") == "accessory"


def test_same_words_are_junk_where_the_whole_device_is_sold():
    assert junk_reason("Системный блок", "корпус без комплектующих", "pc") == "incomplete"
    assert junk_reason("GTX 1060 6gb", "без кулера, охлаждение отсутствует", "videokarty") == "incomplete"
    assert junk_reason("Коробка от процессора Ryzen 5", "", "protsessory") == "box"            # коробка — мусор везде
    assert junk_reason("WD Purple 12 тб", "неисправный, на запчасти", "zhestkie_diski") == "scrap"


@pytest.mark.parametrize("title,desc", [
    ("3070 ti asus rog strix", "Коробка в наличии. Покупал в 2022."),
    ("RTX3060 12gb Gigabyte Eagle OC", "С пломбой, без коробки. Возможна проверка"),
    ("Nvidia GT 520 1gb", "Продам бесшумную, без вентиляторов видеокарту"),
    ("RTX 3070", "утилита MSI Afterburner в комплекте не нужна, работает тихо"),
    ("Игровой пк без видеокарты", "Ryzen 5 5600, B550"),
    ("Оперативная память ddr4 16gb Crucial", "Наклейка сорвана, спеки в утилитах проверены. Тест Аиды приложил."),
    ("Видеокарта RTX 3060 12GB", "кабель питания в комплекте, мост не нужен"),
])
def test_normal_listings_are_not_junk(title, desc):
    assert junk_reason(title, desc) == ""


def test_junk_is_kept_out_of_market_and_candidates():
    conn = db.connect(":memory:")
    put(conn, [L(i, 27000 + (i % 3) * 300) for i in range(12)])
    st = put(conn, [L(500, 15000, "Видеокарта RTX 3070 8GB", "Охлаждение отсутствует, сломалось"),
                    L(501, 500, "Коробка от RTX 3070 8GB")], NOW + 60)
    assert st["candidates"] == 0 and st["modes"]["market"]["rejected"] == {"junk": 2}
    rows = {r["id"]: r["junk"] for r in conn.execute("SELECT id, junk FROM listings WHERE id IN ('500','501')")}
    assert rows == {"500": "incomplete", "501": "box"}
    ms = db.market_stats(conn, "RTX 3070", None, NOW + 120)
    assert ms.n == 12 and ms.median_ask == 27300                       # мусор рынок не двигает


def test_placeholder_price_becomes_junk():
    conn = db.connect(":memory:")
    put(conn, [L(i, 27000 + (i % 3) * 300) for i in range(12)])
    put(conn, [L(500, 3200, "Видеокарта RTX 3070 8GB", "цену уточняйте")], NOW + 60)
    assert conn.execute("SELECT junk, mode_tags FROM listings WHERE id='500'").fetchone()[:] == ("price", None)


# ---------------------------------------------------------------- новое / б/у
def test_new_or_used_guess():
    assert is_new("Видеокарта RTX 5060 новая", "запечатана, гарантия магазина")
    assert is_new("RTX 5070", "В заводской упаковке, не распакована")
    assert not is_new("RTX 3070", "Состояние как новая, почти новая карта")
    assert not is_new("RTX 3070", "покупал новую в 2022, пользовался год")
    assert not is_new("GTX 1060 6GB", "Обслужена: новая термопаста и новые термопрокладки. Перешёл на новую карту.")
    assert not is_new("RX 580 8GB", "Продаю в связи с покупкой новой. Стоит новый кулер.")
    assert is_new("RTX 5050", "Абсолютно новая, чек и гарантия")
    assert is_new("Память DDR5 32GB Kingston Fury Beast Black RTL Gaming KF556C40BBK2-32", "")   # магазинная коробка
    assert not is_new("Оперативная память ddr5 32gb", "Тайминги 40-40-40, controller")
    assert not is_new("RTX 3070 8GB", "Отличное состояние, не вскрывалась")


def test_market_uses_used_listings_when_there_are_enough():
    conn = db.connect(":memory:")
    used = [L(i, 30000 + (i % 3) * 500) for i in range(8)]
    new = [L(100 + i, 45000, "Видеокарта RTX 3070 8GB новая", "запечатана") for i in range(6)]
    put(conn, used + new)
    ms = db.market_stats(conn, "RTX 3070", None, NOW + 60)
    assert ms.used_only and ms.n == 8 and ms.new_n == 6 and ms.median_ask == 30500      # без новых: 30 500, с ними было бы 31 000
    few = db.connect(":memory:")
    put(few, [L(i, 30000) for i in range(3)] + new)
    assert db.market_stats(few, "RTX 3070", None, NOW + 60).used_only is False            # б/у мало — считаем по всем


# ---------------------------------------------------------------- цена быстрой продажи
def stats(prices, sold=None, sold_n=0):
    import statistics
    p = sorted(prices)
    return db.MarketStats(key="X", n=len(p), median_ask=statistics.median(p), p25_ask=statistics.quantiles(p, n=4)[0],
                          p10_ask=statistics.quantiles(p, n=10)[0], median_sold_price=sold, sold_n=sold_n)


def test_quick_price_is_never_above_median_minus_8pct():
    tight = stats([30000] * 12)
    pts = price_points(tight, Config())
    assert pts.quick == pytest.approx(27600) and pts.basis == "медиана минус 8%" and pts.comfort == pytest.approx(29100)
    assert "просят, а не по которым купили" in pts.why[0] and "Итого" in pts.why[-1]


def test_quick_price_follows_lower_quartile_when_prices_are_spread():
    wide = stats([17000, 17000, 17500, 18000, 20000, 22000, 22000, 24000, 26000, 30000, 30000, 31000])
    pts = price_points(wide, Config())
    assert pts.quick == wide.p25_ask < wide.median_ask * 0.92 and pts.basis == "нижняя четверть запросных цен"


def test_sold_prices_take_over_when_there_are_five():
    s = stats([30000] * 12, sold=25000, sold_n=5)
    assert price_points(s, Config()).quick == 25000 and price_points(s, Config()).basis == "цены ушедших объявлений"
    few = stats([30000] * 12, sold=25000, sold_n=4)
    assert price_points(few, Config()).quick == pytest.approx(27600)                     # четырёх лотов мало
    high = stats([30000] * 12, sold=29500, sold_n=9)
    assert price_points(high, Config()).quick == pytest.approx(27600)                    # вверх оценку не тянем


def test_used_item_is_priced_below_a_new_one():
    """Б/у RTX 5050 просят 46 000, а новую продают от 39 000: быстро б/у уйдёт только заметно дешевле новой."""
    s = stats([37000, 37000, 38000, 38000, 41800, 42333, 43600, 48923, 48973, 51608, 52004, 56221])
    plain = price_points(s, Config())
    assert plain.quick == s.p25_ask > 35100
    s.new_low = 39000.0
    pts = price_points(s, Config())
    assert pts.quick == pytest.approx(35100) and pts.basis == "на 10% дешевле новой"
    assert pts.comfort == pytest.approx(39000 * 0.95) and any("Новую такую продают от 39 000" in w for w in pts.why)
    assert price_points(s, Config(used_vs_new=0)).quick == plain.quick
    cheap = stats([20000] * 12)
    cheap.new_low = 39000.0
    assert price_points(cheap, Config()).quick == pytest.approx(18400)                   # вверх оценку не тянем
    odd = stats([30000] * 12)
    odd.new_low = 24000.0                                                                # «новые» дешевле б/у — метка ошибочна
    assert price_points(odd, Config()).quick == pytest.approx(27600)


def test_new_price_comes_from_new_listings_of_the_same_model():
    conn = db.connect(":memory:")
    put(conn, [L(i, p) for i, p in enumerate((34000, 35000, 38000, 41000, 46000, 47000, 48000, 52000))]
        + [L(100 + i, p, "Видеокарта RTX 3070 8GB новая", "запечатана") for i, p in enumerate((39000, 40900, 42600))])
    ms = db.market_stats(conn, "RTX 3070", None, NOW + 60)
    assert ms.used_only and ms.new_n == 3 and ms.new_low == 39000
    put(conn, [L(500, 30000)], NOW + 120)
    sc = score(conn, 500)
    assert sc["quick_price"] == pytest.approx(35100) and sc["basis"] == "на 10% дешевле новой"
    two = db.connect(":memory:")
    put(two, [L(i, 46000) for i in range(8)] + [L(100 + i, 39000, "Видеокарта RTX 3070 8GB новая", "запечатана") for i in range(2)])
    assert db.market_stats(two, "RTX 3070", None, NOW + 60).new_low == 0                 # двух новых мало


def test_stronger_and_cheaper_model_caps_the_price():
    s = stats([35000] * 12)                                                              # слабая, но дорогая
    pts = price_points(s, Config(), ceiling=(30000.0, "RTX 3070"))
    assert pts.quick == 30000 and pts.ceiling_key == "RTX 3070" and "RTX 3070" in pts.basis
    assert pts.comfort == pytest.approx(31500) and any("мощнее" in w for w in pts.why)
    deep = price_points(stats([70000] * 12), Config(), ceiling=(41000.0, "RTX 3080"))    # потолок вдвое ниже
    assert deep.quick == pytest.approx(56000) and deep.ceiling == 41000                  # режем не глубже 20% от своей медианы
    assert any("ниже не опускаем" in w for w in deep.why)
    assert price_points(s, Config(analog_ceiling=False), ceiling=(30000.0, "RTX 3070")).quick == pytest.approx(32200)
    assert price_points(s, Config(), ceiling=(40000.0, "RTX 3080")).ceiling is None       # сильная дороже — потолка нет


def test_ceilings_come_from_analog_table():
    conn = db.connect(":memory:")
    put(conn, [L(i, 35000, "Видеокарта RTX 4060 8GB") for i in range(8)]                 # индекс 115
        + [L(100 + i, 30000, "Видеокарта RTX 3060 Ti 8GB") for i in range(12)]           # 128
        + [L(200 + i, 29000, "Видеокарта RTX 3070 8GB") for i in range(8)]               # 145, но 8 лотов — медиана ненадёжна
        + [L(300 + i, 28000, "Видеокарта RTX 3060 12GB") for i in range(8)])             # 100 — слабее, не потолок
    caps = analogs.ceilings(conn, NOW + 60)
    assert caps["RTX 4060"] == (30000, "RTX 3060 Ti")          # не 29 000 от RTX 3070: по ней меньше 10 объявлений
    assert "RTX 3060 12GB" not in caps and "RTX 3060 Ti" not in caps
    # лот 4060 за 27 000 раньше выглядел выгодным (рынок 35 000), теперь прибыль считается от 30 000
    put(conn, [L(500, 27000, "Видеокарта RTX 4060 8GB")], NOW + 120)
    sc = score(conn, 500)
    assert sc["quick_price"] == 30000 and sc["ceiling_key"] == "RTX 3060 Ti" and sc["sell_price"] == 30000
    assert sc["net"] < 35000 * 0.92 - 27000 * 0.9 - 400 and sc["comfort_net"] > sc["net"]
    assert conn.execute("SELECT mode_tags FROM listings WHERE id='500'").fetchone()[0] is None


def test_profit_uses_quick_price_and_explains_itself():
    conn = db.connect(":memory:")
    put(conn, [L(i, 27000 + (i % 3) * 300) for i in range(12)])
    put(conn, [L(500, 19000)], NOW + 60)
    sc = score(conn, 500)
    assert sc["quick_price"] == pytest.approx(27300 * 0.92) and sc["sell_price"] == sc["quick_price"]
    # лот на 30% дешевле всех похожих — к расчёту добавлен риск «дешевле всех» (15% × половина цены покупки)
    assert sc["flags"] == ["outlier"] and sc["risk_loss"] == pytest.approx(17100 * 0.15 * 0.5)
    assert sc["net"] == pytest.approx(27300 * 0.92 - 17100 - 400 - 17100 * 0.001 * 14 - 17100 * 0.15 * 0.5)
    assert sc["comfort_price"] == pytest.approx(27300 * 0.97) and sc["comfort_net"] > sc["net"]
    assert len(sc["why"]) >= 4 and "Покупка после торга 10%" in sc["why"][-1]
    reason = conn.execute("SELECT reason FROM listings WHERE id='500'").fetchone()[0]
    assert "быстрая продажа ≈ 25 116 ₽" in reason


def test_cheaper_than_everything_gets_extra_risk():
    spread = [20000, 24000, 25000, 26000, 27000, 27000, 27500, 28000, 28000, 29000, 30000, 31000]
    s = stats(spread)
    ok = score_listing(22000, {}, s, Config(max_price=99999))
    low = score_listing(15000, {}, s, Config(max_price=99999))
    assert "outlier" not in ok.flags and "outlier" in low.flags and low.risk_loss > 0
    assert any("ниже почти всех" in w for w in low.why)
