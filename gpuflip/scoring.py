"""Скоринг: ожидаемая чистая прибыль после всех издержек и риска.

Цена продажи — не «медиана объявлений», а осторожная оценка: за сколько вещь реально уйдёт быстро.
Реальных цен сделок Авито не показывает, поэтому опираемся на то, что видно:
  1) запросные цены б/у объявлений той же позиции (медиана и нижняя четверть),
  2) последние цены ушедших объявлений — когда их накопится хотя бы пять,
  3) цены более сильных моделей: слабую карту не продать быстро дороже, чем просят за сильную.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .config import Config
from .db import MarketStats


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


@dataclass
class Score:
    ok: bool
    reason: str                       # "ok" или причина отказа
    ask: float = 0.0
    target_buy: float = 0.0           # во сколько реально рассчитываем купить после торга
    sell_price: float = 0.0           # цена быстрой продажи — от неё считается прибыль
    profit: float = 0.0               # до риска
    risk_loss: float = 0.0
    net: float = 0.0                  # ожидаемая чистая прибыль
    net_pct: float = 0.0
    ask_net: float = 0.0              # чистая прибыль, если брать без торга
    discount_vs_market: float = 0.0   # (медиана - цена)/медиана
    days_to_sell: float = 0.0
    market_n: int = 0
    median_ask: float = 0.0
    flags: list[str] = field(default_factory=list)
    low_data: bool = False            # рынок 6–9 объявлений: оценка есть, алерта нет
    position: str = ""                # по какой позиции считался рынок
    precise: bool = False             # True — по точной позиции (с брендом/частотой)
    days_real: bool = False           # срок продажи посчитан по ушедшим лотам, а не взят условным
    days_n: int = 0                   # по скольким ушедшим
    # --- ценовые ориентиры ---
    quick_price: float = 0.0          # быстрая продажа (около недели)
    comfort_price: float = 0.0        # спокойная продажа (2–4 недели)
    comfort_net: float = 0.0          # чистыми, если продавать спокойно
    p25: float = 0.0                  # нижняя четверть запросных цен
    p10: float = 0.0
    sold_median: float | None = None  # медиана последних цен ушедших объявлений
    sold_n: int = 0
    ceiling: float | None = None      # медиана более сильной модели, если она ниже
    ceiling_key: str = ""
    used_only: bool = False
    new_n: int = 0
    basis: str = ""                   # чем ограничена цена быстрой продажи
    why: list[str] = field(default_factory=list)   # объяснение расчёта человеческим языком

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PricePoints:
    quick: float
    comfort: float
    basis: str
    why: list[str]
    ceiling: float | None = None
    ceiling_key: str = ""


SOLD_MIN = 5      # от стольких ушедших лотов их цены участвуют в оценке


def price_points(stats: MarketStats, cfg: Config, ceiling: tuple[float, str] | None = None) -> PricePoints:
    """Ориентиры цены продажи для позиции и объяснение, откуда они взялись."""
    med = stats.median_ask
    who = "б/у объявлений" if stats.used_only else "объявлений"
    why = [f"Рынок: {stats.n} {who} этой позиции, медиана запросной цены {_rub(med)}"
           + (f" (ещё {stats.new_n} новых с магазинной ценой не учитываем — перепродавать придётся как б/у)" if stats.used_only else "")
           + ". Это цены, которые просят, а не по которым купили: реальных сделок Авито не показывает."]
    by_median = med * cfg.ask_to_sale
    quick, basis = by_median, f"медиана минус {round((1 - cfg.ask_to_sale) * 100)}%"
    if stats.p25_ask and stats.p25_ask < quick:
        quick, basis = stats.p25_ask, "нижняя четверть запросных цен"
    why.append(f"Чтобы продать быстро, надо встать дешевле большинства: {basis} = {_rub(quick)}"
               + (f" (четверть продавцов просит не больше {_rub(stats.p25_ask)})" if stats.p25_ask else "") + ".")
    if stats.median_sold_price and stats.sold_n >= SOLD_MIN:
        if stats.median_sold_price < quick:
            quick, basis = stats.median_sold_price, "цены ушедших объявлений"
        why.append(f"Ушедшие объявления ({stats.sold_n} шт.) стояли по {_rub(stats.median_sold_price)} — это ближе всего к реальной цене продажи"
                   + (", берём её." if basis == "цены ушедших объявлений" else ", оценка с ней согласуется."))
    else:
        why.append(f"Ушедших объявлений этой позиции пока {stats.sold_n} — проверить оценку фактом нельзя, она по запросным ценам.")
    out = PricePoints(quick, med * cfg.comfort_to_ask, basis, why)
    if ceiling and cfg.analog_ceiling:
        cap, cap_key = ceiling
        if cap < med:
            out.ceiling, out.ceiling_key = cap, cap_key
            why.append(f"{cap_key} мощнее, а просят за неё {_rub(cap)} — дешевле. Покупатель, который сравнивает, возьмёт её, "
                       f"поэтому выше {_rub(cap)} быстро не продать.")
            if cap < out.quick:
                out.quick, out.basis = cap, f"цена более сильной {cap_key}"
            out.comfort = min(out.comfort, max(out.quick, cap * 1.05))
    out.comfort = max(out.comfort, out.quick)
    why.append(f"Итого: быстрая продажа (около недели) ≈ {_rub(out.quick)}, спокойная (2–4 недели) ≈ {_rub(out.comfort)}. "
               "Прибыль считается от быстрой.")
    return out


def _net(buy: float, sell_price: float, days: float, risk_p: float, cfg: Config) -> tuple[float, float, float]:
    sell_net = sell_price * (1 - cfg.sell_fee_pct)
    capital = buy * cfg.capital_cost_per_day * days
    profit = sell_net - buy - cfg.fixed_costs - capital
    risk_loss = risk_p * buy * cfg.risk_loss_share
    return profit, risk_loss, profit - risk_loss


def score_listing(price: float, flags: dict[str, float], stats: MarketStats, cfg: Config,
                  ceiling: tuple[float, str] | None = None) -> Score:
    s = Score(ok=False, reason="", ask=price, flags=sorted(flags), market_n=stats.n, median_ask=stats.median_ask,
              low_data=stats.n < cfg.alert_market_n, position=stats.key, p25=stats.p25_ask, p10=stats.p10_ask,
              sold_median=stats.median_sold_price, sold_n=stats.sold_n, used_only=stats.used_only, new_n=stats.new_n)

    if not price or price <= 0:
        s.reason = "no_price"
        return s
    if stats.n >= cfg.min_market_n:                 # ориентиры показываем всегда, когда рынок есть
        pts = price_points(stats, cfg, ceiling)
        s.quick_price, s.comfort_price, s.basis, s.why = pts.quick, pts.comfort, pts.basis, pts.why
        s.ceiling, s.ceiling_key = pts.ceiling, pts.ceiling_key
        s.sell_price = pts.quick
        s.discount_vs_market = (stats.median_ask - price) / stats.median_ask
    if price < cfg.min_price or price > cfg.max_price:
        s.reason = "out_of_budget"
        return s
    if stats.n < cfg.min_market_n:
        s.reason = f"thin_market({stats.n}<{cfg.min_market_n})"
        return s
    if "prepay" in flags:
        s.reason = "prepayment"
        return s

    s.days_to_sell = stats.days_to_sell or cfg.default_days_to_sell
    s.days_real, s.days_n = stats.days_to_sell is not None, stats.lived_n

    if price < stats.median_ask * cfg.scam_price_ratio:
        s.reason = "too_cheap_scam_risk"
        return s
    if stats.n >= cfg.alert_market_n and stats.p10_ask and price < stats.p10_ask * cfg.outlier_ratio:
        # дешевле почти всех похожих: либо удача, либо с лотом что-то не так — закладываем риск
        flags = {**flags, "outlier": cfg.outlier_penalty}
        s.flags = sorted(flags)
        s.why.append(f"Цена ниже почти всех похожих (10% самых дешёвых начинаются с {_rub(stats.p10_ask)}): "
                     "заложен дополнительный риск — проверьте лот особенно внимательно.")

    risk_p = min(0.6, sum(flags.values()))
    s.target_buy = price * (1 - cfg.negotiation_discount)
    s.profit, s.risk_loss, s.net = _net(s.target_buy, s.sell_price, s.days_to_sell, risk_p, cfg)
    s.net_pct = s.net / s.target_buy if s.target_buy else 0.0
    _, _, s.ask_net = _net(price, s.sell_price, s.days_to_sell, risk_p, cfg)
    _, _, s.comfort_net = _net(s.target_buy, s.comfort_price, max(s.days_to_sell, 21.0), risk_p, cfg)
    s.why.append(f"Покупка после торга {round(cfg.negotiation_discount * 100)}%: {_rub(s.target_buy)}. Минус издержки {_rub(cfg.fixed_costs)}"
                 + (f", минус риск {_rub(s.risk_loss)}" if s.risk_loss else "")
                 + f". Чистыми при быстрой продаже: {_rub(s.net)}, при спокойной: {_rub(s.comfort_net)}.")

    if s.days_to_sell > cfg.max_days_to_sell:
        s.reason = f"illiquid({s.days_to_sell:.0f}d)"
    elif s.net < cfg.min_profit_rub:
        s.reason = "profit_too_small"
    elif s.net_pct < cfg.min_profit_pct:
        s.reason = "margin_too_small"
    elif stats.n < cfg.alert_market_n:
        s.reason = f"low_data({stats.n}<{cfg.alert_market_n})"
    else:
        s.ok, s.reason = True, "ok"
    return s
