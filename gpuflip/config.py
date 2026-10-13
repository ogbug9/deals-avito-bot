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


def set_env(path: str, values: dict[str, str]) -> None:
    """Записать настройки в .env, не трогая остальные строки и комментарии. Пустое значение очищает ключ."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    left = {k: str(v).replace("\n", " ").strip() for k, v in values.items()}
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in left:
            out.append(f"{key}={left.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in left.items()]
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


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
    delay_sec: float = 15.0         # пауза между страницами (случайно x1..x2); быстрее — Авито даёт 429
    max_pages: int = 5
    chrome_profile: str = ".chrome-profile"   # постоянный профиль Chrome для сборщика
    headless: bool = False          # без окна капчу пройти нельзя — по умолчанию окно видно
    captcha_wait: float = 180.0     # сколько секунд ждать, пока человек пройдёт капчу
    # Экономика сделки
    negotiation_discount: float = 0.10   # на сколько реально удаётся сбить цену (консервативно)
    ask_to_sale: float = 0.92            # быстрая продажа — не дороже медианы запросных цен * это
    comfort_to_ask: float = 0.97         # спокойная продажа ≈ медиана * это
    analog_ceiling: bool = True          # не оценивать продажу выше медианы более сильной модели (words/analogs.json)
    outlier_ratio: float = 0.85          # цена ниже 10-го процентиля * это — «дешевле всех», штраф к оценке
    outlier_penalty: float = 0.15
    placeholder_ratio: float = 0.25      # цена ниже этой доли медианы — цена-заглушка, мусор
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
    alert_market_n: int = 10             # алерт — только от стольких; при 6–9 оценка показывается с пометкой «мало данных»
    # Режимы поиска перекосов (0/1 в .env)
    mode_bundles: bool = True            # 1: разбор целых ПК на детали
    mode_regional: bool = True           # 2: дешёвый лот в регионе с Авито Доставкой против московской медианы
    mode_badtext: bool = True            # 3: модель из описания / с опечаткой
    mode_urgent: bool = True             # 4: срочные продажи — ниже порог прибыли
    mode_categories: bool = True         # 5: ОЗУ, процессоры, БП, диски
    regions: list[str] = field(default_factory=list)   # REGIONS=sankt-peterburg,kazan — доп. регионы для режима 2
    region_max_pages: int = 2
    delivery_cost: float = 500.0         # доставка из региона, ₽
    urgent_profit_pct: float = 0.10      # порог чистой прибыли для срочных (вместо min_profit_pct)
    fuzzy_max_ratio: float = 0.60        # fuzzy-модель: цена не выше этой доли медианы
    bundle_min_markup: float = 0.25      # ПК: сумма деталей должна быть выше цены лота минимум на столько
    bundle_days: float = 14.0            # разборка и продажа по частям
    recheck_per_cycle: int = 3           # сколько карточек перепроверять за обход (кандидаты + случайные)
    deep_per_cycle: int = 3              # сколько карточек читать целиком за обход (прошедшие предфильтр по цене)
    verify_per_cycle: int = 5            # сколько давно не виденных объявлений проверять за обход: живо или ушло
    stale_days: float = 3.0              # «давно не видели»: столько дней объявление не попадалось в выдаче
    deep_discount: float = 0.10          # предфильтр: цена ниже медианы минимум на столько
    reseller_penalty: float = 0.10       # штраф риска продавцу с множеством одинаковых лотов
    # Лёгкий цикл и защита от блокировок
    light_cycle: bool = True             # раз в несколько минут смотреть 1-ю страницу приоритетных источников
    light_min_sec: float = 180.0
    light_max_sec: float = 300.0
    full_interval_sec: float = 3600.0    # полный обход всех источников
    max_requests_per_hour: int = 45      # потолок загрузок страниц Авито в час (выдача + карточки)
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
        c.regions = [r.strip() for r in env.get("REGIONS", "").split(",") if r.strip()]
        for name in ("bundles", "regional", "badtext", "urgent", "categories"):
            v = env.get(f"MODE_{name.upper()}")
            if v not in (None, ""):
                setattr(c, f"mode_{name}", v.strip().lower() in ("1", "true", "yes", "on"))
        if env.get("ANALOG_CEILING") not in (None, ""):
            c.analog_ceiling = env["ANALOG_CEILING"].strip().lower() in ("1", "true", "yes", "on")
        if env.get("LIGHT_CYCLE") not in (None, ""):
            c.light_cycle = env["LIGHT_CYCLE"].strip().lower() in ("1", "true", "yes", "on")
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
            ("ALERT_MARKET_N", "alert_market_n", int), ("REGION_MAX_PAGES", "region_max_pages", int),
            ("DELIVERY_COST", "delivery_cost", float), ("RECHECK_PER_CYCLE", "recheck_per_cycle", int),
            ("DEEP_PER_CYCLE", "deep_per_cycle", int), ("VERIFY_PER_CYCLE", "verify_per_cycle", int),
            ("STALE_DAYS", "stale_days", float), ("LIGHT_MIN_SEC", "light_min_sec", float),
            ("LIGHT_MAX_SEC", "light_max_sec", float), ("FULL_INTERVAL_SEC", "full_interval_sec", float),
            ("MAX_REQUESTS_PER_HOUR", "max_requests_per_hour", int),
        ]:
            f(name, attr, cast)
        return c
