"""Конфигурация из переменных окружения / .env. Все пороги — стартовые, подбираются по данным."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = re.split(r"\s+#", v, maxsplit=1)[0]   # комментарий в конце строки: «40000   # потолок»
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class Config:
    # Telegram
    tg_token: str = ""
    tg_chat_id: str = ""
    # Хранилище
    db_path: str = "gpuflip.db"
    # Поиск
    search_urls: list[str] = field(default_factory=list)
    proxy: str = ""
    delay_sec: float = 6.0          # пауза между страницами (случайно x1..x2)
    max_pages: int = 5
    chrome_profile: str = ".chrome-profile"   # постоянный профиль Chrome для сборщика
    headless: bool = False          # без окна капчу пройти нельзя — по умолчанию окно видно
    captcha_wait: float = 180.0     # сколько секунд ждать, пока человек пройдёт капчу
    # Экономика сделки
    negotiation_discount: float = 0.10   # на сколько реально удаётся сбить цену (консервативно)
    ask_to_sale: float = 0.92            # цена продажи ≈ медиана запросных цен * это
    sell_fee_pct: float = 0.0            # комиссия при продаже (Авито Доставка и т.п.)
    fixed_costs: float = 400.0           # тест, упаковка, доставка, ₽ на сделку
    capital_cost_per_day: float = 0.001  # стоимость замороженных денег, доля в день
    default_days_to_sell: float = 14.0
    risk_loss_share: float = 0.5         # какую долю цены теряем, если сделка «плохая»
    # Фильтры отбора
    min_profit_rub: float = 1500.0
    min_profit_pct: float = 0.15
    min_price: float = 3000.0
    max_price: float = 40000.0           # под размер оборотных денег
    min_market_n: int = 6                # минимум похожих объявлений для оценки рынка
    max_days_to_sell: float = 21.0
    scam_price_ratio: float = 0.45       # цена ниже этой доли рынка — подозрение на развод
    max_alerts_per_scan: int = 10
    realert_drop_pct: float = 0.10

    @classmethod
    def load(cls, env_file: str = ".env") -> "Config":
        env = {**_read_env_file(env_file), **os.environ}
        c = cls()

        def f(name: str, attr: str, cast=float):
            if env.get(name) not in (None, ""):
                setattr(c, attr, cast(env[name]))

        c.tg_token = env.get("TG_TOKEN", "")
        c.tg_chat_id = env.get("TG_CHAT_ID", "")
        c.db_path = env.get("DB_PATH", c.db_path)
        c.proxy = env.get("SCRAPE_PROXY", "")
        c.chrome_profile = env.get("CHROME_PROFILE", c.chrome_profile)
        c.headless = env.get("HEADLESS", "").lower() in ("1", "true", "yes")
        urls = env.get("SEARCH_URLS", "")
        c.search_urls = [u.strip() for u in urls.split(",") if u.strip()]
        for name, attr, cast in [
            ("DELAY_SEC", "delay_sec", float), ("MAX_PAGES", "max_pages", int),
            ("CAPTCHA_WAIT", "captcha_wait", float),
            ("NEGOTIATION_DISCOUNT", "negotiation_discount", float),
            ("ASK_TO_SALE", "ask_to_sale", float), ("SELL_FEE_PCT", "sell_fee_pct", float),
            ("FIXED_COSTS", "fixed_costs", float),
            ("CAPITAL_COST_PER_DAY", "capital_cost_per_day", float),
            ("MIN_PROFIT_RUB", "min_profit_rub", float), ("MIN_PROFIT_PCT", "min_profit_pct", float),
            ("MIN_PRICE", "min_price", float), ("MAX_PRICE", "max_price", float),
            ("MIN_MARKET_N", "min_market_n", int), ("MAX_DAYS_TO_SELL", "max_days_to_sell", float),
            ("MAX_ALERTS_PER_SCAN", "max_alerts_per_scan", int),
        ]:
            f(name, attr, cast)
        return c
