import json

from gpuflip import db
from gpuflip.collector import RawListing, parse_search_html, read_csv, with_page
from gpuflip.config import Config
from gpuflip.notifier import Notifier
from gpuflip.pipeline import process
from gpuflip.scoring import score_listing

DAY = 86400


def cfg(**kw):
    c = Config(min_market_n=5, fixed_costs=400)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def mk(i, price, title="Видеокарта RTX 3060 Ti 8GB", desc=""):
    return RawListing(id=str(i), title=title, price=price, url=f"https://x/{i}", description=desc)


def market(n=8, price=27000):
    return [mk(i, price + (i % 3) * 300) for i in range(n)]


def test_market_stats_and_leave_one_out(tmp_path):
    conn = db.connect(":memory:")
    process(conn, cfg(), market(), "s", True, now=1000.0)
    ms = db.market_stats(conn, "RTX 3060 Ti", "0", 1000.0)
    assert ms.n == 7 and 26900 < ms.median_ask < 27700


def test_outlier_trimmed():
    conn = db.connect(":memory:")
    items = market() + [mk(100, 90000)]
    process(conn, cfg(), items, "s", True, now=1000.0)
    ms = db.market_stats(conn, "RTX 3060 Ti", None, 1000.0)
    assert ms.n == 8 and ms.median_ask < 28000


def test_gone_and_liquidity():
    conn = db.connect(":memory:")
    process(conn, cfg(), [mk(900, 26000)], "s", True, now=0.0)          # первый обход = baseline
    items = market(8)
    process(conn, cfg(), items + [mk(900, 26000)], "s", True, now=1 * DAY)   # новые, возраст известен
    # через 5 дней 4 новых объявления пропали при полном обходе
    st = process(conn, cfg(), items[4:] + [mk(900, 26000)], "s", True, now=5 * DAY)
    assert st["gone"] == 4
    ms = db.market_stats(conn, "RTX 3060 Ti", None, 5 * DAY)
    assert ms.sold_n == 4 and abs(ms.days_to_sell - 4) < 0.01


def test_baseline_not_used_for_liquidity():
    conn = db.connect(":memory:")
    items = market(8)
    process(conn, cfg(), items, "s", True, now=0.0)
    process(conn, cfg(), items[4:], "s", True, now=3 * DAY)
    ms = db.market_stats(conn, "RTX 3060 Ti", None, 3 * DAY)
    assert ms.sold_n == 4 and ms.days_to_sell is None


def test_incomplete_scan_does_not_mark_gone():
    conn = db.connect(":memory:")
    items = market(8)
    process(conn, cfg(), items, "s", True, now=0.0)
    st = process(conn, cfg(), items[4:], "s", False, now=DAY)
    assert st["gone"] == 0


def test_alert_for_cheap_and_dedupe():
    conn = db.connect(":memory:")
    process(conn, cfg(), market(), "s", True, now=1000.0)
    out = []
    n = Notifier(out=out.append)
    deal = mk(500, 19000)
    st = process(conn, cfg(), market() + [deal], "s", True, notifier=n, now=1100.0)
    assert st["alerts"] == 1 and "RTX 3060 Ti" in out[0] and "19 000" in out[0]
    st = process(conn, cfg(), market() + [deal], "s", True, notifier=n, now=1200.0)
    assert st["alerts"] == 0                         # не спамим повторно
    cheaper = mk(500, 16500)
    st = process(conn, cfg(), market() + [cheaper], "s", True, notifier=n, now=1300.0)
    assert st["alerts"] == 1                         # цена упала >10% — повтор


def test_thin_market_no_alert():
    conn = db.connect(":memory:")
    st = process(conn, cfg(), [mk(1, 27000), mk(2, 15000)], "s", True, now=1.0)
    assert st["alerts"] == 0


def test_scam_prepay_and_defects_rejected():
    conn = db.connect(":memory:")
    process(conn, cfg(), market(), "s", True, now=1000.0)
    ms = db.market_stats(conn, "RTX 3060 Ti", None, 1000.0)
    assert score_listing(8000, {}, ms, cfg()).reason == "too_cheap_scam_risk"
    assert score_listing(19000, {"prepay": .4}, ms, cfg()).reason == "prepayment"
    assert score_listing(26500, {}, ms, cfg()).reason in ("profit_too_small", "margin_too_small")
    ok = score_listing(19000, {}, ms, cfg())
    assert ok.ok and ok.net > 1500 and ok.net_pct > 0.15


def test_risk_flags_reduce_profit():
    conn = db.connect(":memory:")
    process(conn, cfg(), market(), "s", True, now=1000.0)
    ms = db.market_stats(conn, "RTX 3060 Ti", None, 1000.0)
    clean = score_listing(20000, {}, ms, cfg())
    risky = score_listing(20000, {"untested": .15, "mining": .08}, ms, cfg())
    assert risky.net < clean.net


def test_deals():
    conn = db.connect(":memory:")
    d = db.deal_buy(conn, "1", 20000)
    assert db.deal_sell(conn, d, 26000, costs=500)
    s = db.deal_stats(conn)
    assert s["closed"] == 1 and s["profit"] == 5500


HTML = """
<div data-marker="item" data-item-id="123">
  <a data-marker="item-title" href="/moskva/tovary/rtx_3060_ti_123"><h3 itemprop="name">RTX 3060 Ti 8GB</h3></a>
  <meta itemprop="price" content="21500">
  <div class="iva-item-description-x">Отличное состояние</div>
  <div data-marker="item-address">Москва</div>
</div>
<div data-marker="item" data-item-id="124">
  <a data-marker="item-title" href="https://www.avito.ru/x/124"><h3 itemprop="name">GTX 1070</h3></a>
  <span data-marker="item-price">9 500 ₽</span>
</div>
"""


def test_parse_avito_html():
    items = parse_search_html(HTML)
    assert [i.id for i in items] == ["123", "124"]
    assert items[0].price == 21500 and items[0].url.startswith("https://www.avito.ru/")
    assert items[1].price == 9500 and items[0].city == "Москва"


def test_with_page():
    assert with_page("https://www.avito.ru/moskva?q=rtx&s=104", 3).endswith("p=3")


def test_csv(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("title,price,url\nRTX 3060 Ti 8GB,\"21 500\",https://x/1\n,,\n", encoding="utf-8")
    items = read_csv(str(f))
    assert len(items) == 1 and items[0].price == 21500


def test_score_json_saved():
    conn = db.connect(":memory:")
    process(conn, cfg(), market() + [mk(500, 19000)], "s", True, now=1000.0)
    r = conn.execute("SELECT net_profit, score_json FROM listings WHERE id='500'").fetchone()
    assert r["net_profit"] > 0 and json.loads(r["score_json"])["ok"]


def test_env_inline_comments(tmp_path, monkeypatch):
    for k in ("MAX_PRICE", "SEARCH_URLS", "TG_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("MAX_PRICE=40000        # потолок\nSEARCH_URLS=https://a.ru/x?s=104#frag\nTG_TOKEN=\n",
                   encoding="utf-8")
    c = Config.load(str(env))
    assert c.max_price == 40000 and c.search_urls == ["https://a.ru/x?s=104#frag"]
