"""Скоринг: ожидаемая чистая прибыль после всех издержек и риска."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .config import Config
from .db import MarketStats


@dataclass
class Score:
    ok: bool
    reason: str                       # "ok" или причина отказа
    ask: float = 0.0
    target_buy: float = 0.0           # во сколько реально рассчитываем купить после торга
    sell_price: float = 0.0           # оценка цены продажи
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

    def to_dict(self) -> dict:
        return asdict(self)


def _net(buy: float, sell_price: float, days: float, risk_p: float, cfg: Config) -> tuple[float, float, float]:
    sell_net = sell_price * (1 - cfg.sell_fee_pct)
    capital = buy * cfg.capital_cost_per_day * days
    profit = sell_net - buy - cfg.fixed_costs - capital
    risk_loss = risk_p * buy * cfg.risk_loss_share
    return profit, risk_loss, profit - risk_loss


def score_listing(price: float, flags: dict[str, float], stats: MarketStats, cfg: Config) -> Score:
    s = Score(ok=False, reason="", ask=price, flags=sorted(flags), market_n=stats.n, median_ask=stats.median_ask,
              low_data=stats.n < cfg.alert_market_n)

    if not price or price <= 0:
        s.reason = "no_price"
        return s
    if price < cfg.min_price or price > cfg.max_price:
        s.reason = "out_of_budget"
        return s
    if stats.n < cfg.min_market_n:
        s.reason = f"thin_market({stats.n}<{cfg.min_market_n})"
        return s
    if "prepay" in flags:
        s.reason = "prepayment"
        return s

    if stats.median_sold_price:
        s.sell_price = min(stats.median_sold_price, stats.median_ask * cfg.ask_to_sale * 1.05)
    else:
        s.sell_price = stats.median_ask * cfg.ask_to_sale
    s.days_to_sell = stats.days_to_sell or cfg.default_days_to_sell
    s.discount_vs_market = (stats.median_ask - price) / stats.median_ask

    if price < stats.median_ask * cfg.scam_price_ratio:
        s.reason = "too_cheap_scam_risk"
        return s

    risk_p = min(0.6, sum(flags.values()))
    s.target_buy = price * (1 - cfg.negotiation_discount)
    s.profit, s.risk_loss, s.net = _net(s.target_buy, s.sell_price, s.days_to_sell, risk_p, cfg)
    s.net_pct = s.net / s.target_buy if s.target_buy else 0.0
    _, _, s.ask_net = _net(price, s.sell_price, s.days_to_sell, risk_p, cfg)

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
