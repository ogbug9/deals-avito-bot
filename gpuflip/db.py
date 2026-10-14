"""SQLite-хранилище: объявления, оповещения, сделки."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings(
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT '',
  url TEXT, title TEXT, description TEXT, city TEXT,
  price REAL, first_price REAL,
  first_seen REAL, last_seen REAL,
  status TEXT NOT NULL DEFAULT 'active',     -- active | inactive
  gone_at REAL,
  baseline INTEGER NOT NULL DEFAULT 0,       -- 1 = увидели при первом обходе источника, возраст неизвестен
  kind TEXT, skip_reason TEXT, model TEXT, vram INTEGER, gpu_key TEXT,
  flags TEXT,                                -- json
  net_profit REAL, score_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_key ON listings(gpu_key, status);
CREATE TABLE IF NOT EXISTS alerts(
  listing_id TEXT, price REAL, net_profit REAL, sent_at REAL
);
CREATE TABLE IF NOT EXISTS deals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id TEXT, gpu_key TEXT,
  buy_price REAL, bought_at REAL,
  sell_price REAL, sold_at REAL,
  costs REAL DEFAULT 0, note TEXT
);
-- кандидат = объявление × режим поиска; по этой таблице считается отчёт report --by-mode
CREATE TABLE IF NOT EXISTS candidates(
  listing_id TEXT NOT NULL, mode TEXT NOT NULL,
  first_at REAL, last_at REAL, price REAL,
  est_profit REAL, est_profit_pct REAL, reason TEXT, confidence TEXT,
  checked_at REAL, inactive_at REAL,
  PRIMARY KEY(listing_id, mode)
);
CREATE TABLE IF NOT EXISTS mode_stats(
  ts REAL, source TEXT, mode TEXT, viewed INTEGER, candidates INTEGER, rejected TEXT   -- rejected: json {причина: n}
);
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started REAL, finished REAL, source TEXT, seen INTEGER DEFAULT 0, new INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0, candidates INTEGER DEFAULT 0, captcha INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS price_history(listing_id TEXT, ts REAL, price REAL);
CREATE INDEX IF NOT EXISTS ix_ph ON price_history(listing_id, ts);
CREATE TABLE IF NOT EXISTS market_history(day TEXT, key TEXT, median REAL, n INTEGER, PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS saved_searches(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, query TEXT, created REAL);
"""
CREATE_V4 = """
CREATE TABLE IF NOT EXISTS excluded(kind TEXT NOT NULL, value TEXT NOT NULL, title TEXT, created REAL, PRIMARY KEY(kind, value));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS compare_sets(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, ids TEXT, ref TEXT, created REAL);
"""
SCHEMA_VERSION = 6


# Колонки, добавленные после первой версии: дописываются в существующую базу при подключении.
EXTRA_COLUMNS = {
    "category": "TEXT NOT NULL DEFAULT ''",   # ключ из sources.CATEGORIES
    "region": "TEXT NOT NULL DEFAULT ''",     # регион источника (moskva, all, …)
    "delivery": "INTEGER NOT NULL DEFAULT 0",
    "image": "TEXT",
    "date_text": "TEXT",
    "posted_at": "REAL",                      # оценка времени публикации по «N часов назад»
    "rating": "REAL", "reviews": "INTEGER",
    "defect": "INTEGER NOT NULL DEFAULT 0",   # брак/поломка по тексту
    "wanted": "INTEGER NOT NULL DEFAULT 0",   # «куплю», а не «продам»
    # --- версия 3: режимы поиска ---
    "item_key": "TEXT",                       # позиция для медианы в любой категории (для видеокарт = gpu_key)
    "qty": "REAL", "unit_price": "REAL", "brand": "TEXT",   # ГБ/Вт и цена за единицу
    "model_source": "TEXT",                   # title | description | fuzzy
    "urgent": "INTEGER NOT NULL DEFAULT 0",
    "is_msk": "INTEGER NOT NULL DEFAULT 0",   # продавец в Москве
    "weekday": "INTEGER",                     # день недели публикации, 0 = понедельник
    "avito_condition": "TEXT",                # «Новое» / «Б/у» с карточки (заполняется при перепроверке)
    "mode_tags": "TEXT", "est_profit": "REAL", "est_profit_pct": "REAL", "reason": "TEXT", "confidence": "TEXT",
    "checked_at": "REAL",                     # когда карточку открывали для перепроверки
    "fav": "INTEGER NOT NULL DEFAULT 0", "user_status": "TEXT", "note": "TEXT",   # пометки пользователя
    # --- версия 4 ---
    "fine_key": "TEXT",                       # точная позиция: с брендом/частотой (берётся, если в ней хватает данных)
    "seller_id": "TEXT", "seller_name": "TEXT", "seller_type": "TEXT",
    "seller_ads": "INTEGER",                  # сколько активных объявлений этого продавца в нашей базе
    "reseller": "INTEGER NOT NULL DEFAULT 0",  # много одинаковых лотов у продавца / дубли
    "photos": "TEXT",                         # json-список фото с карточки
    "deep_at": "REAL",                        # когда карточку читали целиком (кэш)
    "avito_model": "TEXT",                    # «Бренд / Модель» из характеристик карточки
    "checklist": "TEXT",                      # json: отмеченные пункты проверки на месте
    # --- версия 5 ---
    "junk": "TEXT",                           # мусор: box | incomplete | scrap | price (см. normalizer.JUNK_RU)
    "cond": "TEXT",                           # оценка по тексту: new | used
    # --- версия 6 ---
    "parts_json": "TEXT",                     # разбор ПК на детали с оценкой каждой (pcparts.breakdown)
    "parts_value": "REAL",                    # сумма оценённых деталей по цене быстрой продажи
}
# колонки, добавленные в другие таблицы
EXTRA_OTHER = {
    "deals": {"title": "TEXT", "item_key": "TEXT", "est_profit": "REAL", "url": "TEXT"},
    "saved_searches": {"notify": "INTEGER NOT NULL DEFAULT 0", "notified_at": "REAL"},
}


def backup(path: str) -> str | None:
    """Копия файла базы перед изменением схемы: backups/<имя>-<дата>.db рядом с базой."""
    src = Path(path)
    if path == ":memory:" or not src.exists() or src.stat().st_size == 0:
        return None
    dst_dir = src.parent / "backups"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f"{src.stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    a, b = sqlite3.connect(path, timeout=30), sqlite3.connect(str(dst))
    try:
        a.backup(b)        # согласованная копия, даже если сборщик сейчас пишет
    finally:
        a.close()
        b.close()
    return str(dst)


def auto_backup(path: str, keep: int = 7, every: float = 86400.0, now: float | None = None) -> str | None:
    """Суточная копия базы с ротацией: backups/auto-ГГГГММДД-ЧЧММСС.db, хранится keep последних."""
    return rolling_backup(path, "auto", keep, every, now)


def rolling_backup(path: str, prefix: str, keep: int, every: float, now: float | None = None) -> str | None:
    """Копия базы раз в every секунд с ротацией: backups/<prefix>-<имя>-<дата>.db, хранится keep последних.
    Повреждённую базу не копирует — иначе ротация вытеснила бы целые копии испорченными."""
    from .guard import check_db
    src = Path(path)
    if path == ":memory:" or not src.exists():
        return None
    now = time.time() if now is None else now
    d = src.parent / "backups"
    d.mkdir(exist_ok=True)
    autos = sorted(d.glob(f"{prefix}-{src.stem}-*.db"))
    if autos and now - autos[-1].stat().st_mtime < every:
        return None
    if check_db(src):
        return None
    dst = d / f"{prefix}-{src.stem}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}.db"
    a, b = sqlite3.connect(path, timeout=30), sqlite3.connect(str(dst))
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()
    for old in sorted(d.glob(f"{prefix}-{src.stem}-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return str(dst)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("lc", 1, lambda s: (s or "").lower())   # LIKE в SQLite не умеет регистр кириллицы
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        has_data = conn.execute("SELECT 1 FROM sqlite_master WHERE name='listings'").fetchone() is not None
        if has_data:
            saved = backup(path)
            if saved:
                print(f"База обновляется до версии {SCHEMA_VERSION}; копия: {saved}", flush=True)
        conn.executescript(SCHEMA)
        conn.executescript(CREATE_V4)
        for table, cols in {"listings": EXTRA_COLUMNS, **EXTRA_OTHER}.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_fine ON listings(fine_key, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_seller ON listings(seller_id, status)")
        conn.execute("UPDATE listings SET status='inactive' WHERE status='gone'")
        conn.execute("UPDATE listings SET item_key=gpu_key WHERE item_key IS NULL AND gpu_key IS NOT NULL")
        conn.execute("UPDATE listings SET is_msk=1 WHERE region='moskva'")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_item ON listings(item_key, status)")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")   # интерфейс читает базу, пока сборщик пишет
    return conn


def is_first_scan(conn, source: str) -> bool:
    return conn.execute("SELECT 1 FROM listings WHERE source=? LIMIT 1", (source,)).fetchone() is None


def is_moscow(region: str, city: str, url: str = "") -> bool:
    from .sources import city_from_url
    slug = city_from_url(url)[0]
    return region == "moskva" or slug.startswith("moskva") or "москв" in (city or "").lower()


def upsert_listing(conn, l, source: str, now: float, baseline: bool = False,
                   category: str = "", region: str = "") -> tuple[bool, bool]:
    """Возвращает (новое, цена_изменилась)."""
    age = getattr(l, "age", None)
    posted = now - age if age is not None else None
    weekday = time.localtime(posted).tm_wday if posted and posted > 86400 else None
    msk = int(is_moscow(region, l.city, l.url))
    if not l.city:                    # общероссийская выдача города не показывает — берём из адреса объявления
        from .sources import city_from_url
        l.city = city_from_url(l.url)[1]
    extra = (int(getattr(l, "delivery", False)), getattr(l, "image", "") or None, getattr(l, "date_text", "") or None,
             getattr(l, "rating", None), getattr(l, "reviews", None))
    seller = (getattr(l, "seller_id", "") or None, getattr(l, "seller_name", "") or None)
    row = conn.execute("SELECT price, status FROM listings WHERE id=?", (l.id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO listings(id,source,url,title,description,city,price,first_price,first_seen,last_seen,baseline,"
            "category,region,delivery,image,date_text,rating,reviews,posted_at,weekday,is_msk,seller_id,seller_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (l.id, source, l.url, l.title, l.description, l.city, l.price, l.price, now, now, int(baseline),
             category, region, *extra, posted, weekday, msk, *seller),
        )
        conn.execute("INSERT INTO price_history VALUES(?,?,?)", (l.id, now, l.price))
        return True, False
    changed = row["price"] != l.price
    if changed:
        conn.execute("INSERT INTO price_history VALUES(?,?,?)", (l.id, now, l.price))
    conn.execute(
        "UPDATE listings SET price=?, last_seen=?, status='active', gone_at=NULL, title=?, "
        "description=CASE WHEN length(?)>=length(COALESCE(description,'')) THEN ? ELSE description END, source=?, "
        "city=CASE WHEN ?<>'' THEN ? ELSE city END, "
        "category=CASE WHEN ?<>'' THEN ? ELSE category END, region=CASE WHEN ?<>'' THEN ? ELSE region END, "
        "delivery=MAX(delivery, ?), image=COALESCE(?, image), date_text=COALESCE(?, date_text), "
        "rating=COALESCE(?, rating), reviews=COALESCE(?, reviews), posted_at=COALESCE(posted_at, ?), "
        "weekday=COALESCE(weekday, ?), is_msk=MAX(is_msk, ?), seller_id=COALESCE(?, seller_id), "
        "seller_name=COALESCE(?, seller_name) WHERE id=?",
        (l.price, now, l.title, l.description or "", l.description or "", source, l.city or "", l.city or "",
         category, category, region, region, *extra, posted, weekday, msk, *seller, l.id),
    )
    return False, changed


def mark_gone(conn, source: str, scan_start: float) -> int:
    cur = conn.execute(
        "UPDATE listings SET status='inactive', gone_at=? WHERE source=? AND status='active' AND last_seen<?",
        (scan_start, source, scan_start),
    )
    return cur.rowcount


def save_parse(conn, listing_id: str, p, defect: bool = False, wanted: bool = False, part=None, urgent: bool = False,
               junk: str = "", cond: str = "used") -> None:
    """p — normalizer.Parsed; part — parts.Part для не-видеокарт (позиция, количество, бренд)."""
    conn.execute("UPDATE listings SET junk=?, cond=? WHERE id=?", (junk or None, cond, listing_id))
    item_key = (part.key if part else p.key) or None
    qty = part.qty if part and part.qty else None
    fine = (getattr(part, "fine_key", "") if part else "") or None
    conn.execute(
        "UPDATE listings SET kind=?, skip_reason=?, model=?, vram=?, gpu_key=?, flags=?, defect=?, wanted=?, "
        "item_key=?, fine_key=?, qty=?, unit_price=CASE WHEN ? IS NOT NULL AND price>0 THEN price/? END, brand=?, "
        "model_source=?, urgent=? WHERE id=?",
        (p.kind, p.skip_reason, p.model, p.vram, p.key or None, json.dumps(p.flags), int(defect), int(wanted),
         item_key, fine, qty, qty, qty, part.brand if part else None, getattr(p, "model_source", "") or None, int(urgent),
         listing_id),
    )


def save_score(conn, listing_id: str, net: float | None, score_json: str | None) -> None:
    conn.execute("UPDATE listings SET net_profit=?, score_json=? WHERE id=?", (net, score_json, listing_id))


@dataclass
class MarketStats:
    key: str
    n: int = 0
    median_ask: float = 0.0
    p25_ask: float = 0.0
    p10_ask: float = 0.0
    used_only: bool = False               # рынок посчитан только по б/у (новые отброшены)
    new_n: int = 0                        # сколько объявлений похожи на новые
    new_low: float = 0.0                  # от скольки продают новую (нижняя четверть цен новых), 0 — новых мало
    sold_n: int = 0
    median_sold_price: float | None = None
    days_to_sell: float | None = None     # медиана «жизни» ушедших объявлений, дни
    lived_n: int = 0                      # по скольким ушедшим она посчитана


MIN_USED = 6      # столько б/у объявлений нужно, чтобы считать рынок только по ним
NEW_MIN = 3       # от стольких новых объявлений их цена ограничивает оценку б/у


def _trim(prices: list[float]) -> list[float]:
    if len(prices) < 4:
        return prices
    med = statistics.median(prices)
    kept = [p for p in prices if 0.5 * med <= p <= 1.8 * med]
    return kept or prices


def market_stats(conn, key: str, exclude_id: str | None, now: float,
                 active_window_days: float = 14, sold_window_days: float = 60, msk: bool | None = None,
                 column: str = "item_key") -> MarketStats:
    """msk=True — только московские объявления, False — только региональные, None — все.
    column — item_key (обычная позиция) или fine_key (точная, с брендом)."""
    assert column in ("item_key", "fine_key")
    st = MarketStats(key=key)
    group = "" if msk is None else f" AND is_msk={int(msk)}"
    rows = conn.execute(
        f"SELECT price, cond FROM listings WHERE {column}=? AND status='active' AND id<>? AND last_seen>=? "
        "AND price>0 AND defect=0 AND wanted=0 AND junk IS NULL AND (flags IS NULL OR flags NOT LIKE '%prepay%')" + group,
        (key, exclude_id or "", now - active_window_days * 86400),
    ).fetchall()
    # Перепродаём мы всегда б/у вещь, поэтому сравниваем с б/у: новые (с гарантией магазина) стоят дороже.
    used = [r["price"] for r in rows if r["cond"] != "new"]
    st.new_n = len(rows) - len(used)
    fresh = sorted(r["price"] for r in rows if r["cond"] == "new")
    if len(fresh) >= NEW_MIN:
        st.new_low = statistics.quantiles(fresh, n=4)[0] if len(fresh) >= 4 else fresh[0]
    st.used_only = st.new_n > 0 and len(used) >= MIN_USED
    prices = _trim(sorted(used if st.used_only else [r["price"] for r in rows]))
    st.n = len(prices)
    if prices:
        st.median_ask = statistics.median(prices)
        st.p25_ask = statistics.quantiles(prices, n=4)[0] if len(prices) >= 4 else prices[0]
        st.p10_ask = statistics.quantiles(prices, n=10)[0] if len(prices) >= 10 else prices[0]
    gone = conn.execute(
        "SELECT price, first_seen, posted_at, gone_at, baseline FROM listings "
        f"WHERE {column}=? AND status='inactive' AND gone_at>=? AND defect=0 AND wanted=0 AND junk IS NULL",
        (key, now - sold_window_days * 86400),
    ).fetchall()
    st.sold_n = len(gone)
    if len(gone) >= 3:
        st.median_sold_price = statistics.median(r["price"] for r in gone)
    lived = lifetimes(gone)
    st.lived_n = len(lived)
    if len(lived) >= 3:
        st.days_to_sell = max(0.5, statistics.median(lived))
    return st


def lifetimes(gone_rows) -> list[float]:
    """Сколько дней прожило каждое ушедшее объявление. Если Авито показал дату публикации — от неё;
    иначе от первого обхода, но только если объявление появилось уже при нас (baseline=0)."""
    out = []
    for r in gone_rows:
        start = r["posted_at"] if r["posted_at"] else (None if r["baseline"] else r["first_seen"])
        if start and r["gone_at"] and r["gone_at"] > start:
            out.append((r["gone_at"] - start) / 86400)
    return out


LIQUIDITY_MIN, LIQUIDITY_SOLID = 3, 8      # от 3 ушедших — оценка есть, до 8 — «оценка грубая»


def liquidity(conn, key: str, now: float, column: str = "item_key") -> dict:
    """Рейтинг ликвидности позиции по реальному сроку жизни ушедших объявлений."""
    st = market_stats(conn, key, None, now, column=column)
    if st.days_to_sell is None:
        return {"days": None, "n": st.lived_n, "label": "нет данных", "level": "none", "rough": True}
    d = st.days_to_sell
    level, label = ("high", "высокая") if d <= 5 else ("medium", "средняя") if d <= 14 else ("low", "низкая")
    return {"days": d, "n": st.lived_n, "label": label, "level": level, "rough": st.lived_n < LIQUIDITY_SOLID}


def last_alert(conn, listing_id: str):
    return conn.execute(
        "SELECT price FROM alerts WHERE listing_id=? ORDER BY sent_at DESC LIMIT 1", (listing_id,)
    ).fetchone()


def record_alert(conn, listing_id: str, price: float, net: float, now: float | None = None) -> None:
    conn.execute("INSERT INTO alerts VALUES(?,?,?,?)", (listing_id, price, net, time.time() if now is None else now))


# --- сделки: журнал ---------------------------------------------------------
DEAL_FIELDS = ("title", "item_key", "url", "buy_price", "bought_at", "sell_price", "sold_at", "costs", "note", "est_profit")


def deal_create(conn, listing_id: str | None = None, buy_price: float | None = None, note: str = "",
                title: str = "", costs: float = 0.0, bought_at: float | None = None) -> int:
    """Запись о покупке. Из объявления подтягиваются название, позиция, ссылка и расчётная прибыль на момент покупки."""
    r = conn.execute("SELECT title, item_key, gpu_key, url, price, est_profit FROM listings WHERE id=?",
                     (listing_id,)).fetchone() if listing_id else None
    price = buy_price if buy_price is not None else (r["price"] if r else None)
    if not price or price <= 0:
        raise ValueError("нужна цена покупки")
    cur = conn.execute(
        "INSERT INTO deals(listing_id,gpu_key,title,item_key,url,buy_price,bought_at,costs,note,est_profit) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (listing_id, r["gpu_key"] if r else None, title or (r["title"] if r else "") or "Без названия",
         r["item_key"] if r else None, r["url"] if r else None, float(price),
         time.time() if bought_at is None else bought_at, float(costs or 0), note, r["est_profit"] if r else None))
    if listing_id:
        conn.execute("UPDATE listings SET user_status='bought' WHERE id=?", (listing_id,))
    conn.commit()
    return cur.lastrowid


def deal_update(conn, deal_id: int, **fields) -> bool:
    sets = {k: v for k, v in fields.items() if k in DEAL_FIELDS}
    if not sets:
        return False
    cur = conn.execute(f"UPDATE deals SET {', '.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), deal_id])
    conn.commit()
    return cur.rowcount == 1


def deal_delete(conn, deal_id: int) -> bool:
    cur = conn.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    conn.commit()
    return cur.rowcount == 1


def _deal_row(r, now: float) -> dict:
    d = dict(r)
    closed = r["sell_price"] is not None
    costs = r["costs"] or 0.0
    d["closed"] = closed
    d["days"] = ((r["sold_at"] or now) - r["bought_at"]) / 86400 if r["bought_at"] else None
    d["net"] = r["sell_price"] - r["buy_price"] - costs if closed else None
    d["roi"] = d["net"] / (r["buy_price"] + costs) if closed and (r["buy_price"] + costs) else None
    d["est_gap"] = d["net"] - r["est_profit"] if closed and r["est_profit"] is not None else None
    return d


def deals_list(conn, since: float | None = None, now: float | None = None) -> dict:
    """Журнал и итоги за период (since — по дате покупки). Расчётная прибыль сравнивается с реальной по закрытым."""
    now = time.time() if now is None else now
    rows = [_deal_row(r, now) for r in conn.execute("SELECT * FROM deals WHERE bought_at>=? ORDER BY bought_at DESC, id DESC",
                                                    (since or 0,))]
    closed = [d for d in rows if d["closed"]]
    invested = sum(d["buy_price"] + (d["costs"] or 0) for d in closed)
    net = sum(d["net"] for d in closed)
    with_est = [d for d in closed if d["est_profit"] is not None]
    return {"deals": rows, "summary": {
        "count": len(rows), "closed": len(closed), "open": len(rows) - len(closed),
        "open_money": sum(d["buy_price"] + (d["costs"] or 0) for d in rows if not d["closed"]),
        "invested": invested, "net": net, "roi": net / invested if invested else None,
        "avg_days": statistics.mean(d["days"] for d in closed) if closed else None,
        "est_total": sum(d["est_profit"] for d in with_est) if with_est else None,
        "real_for_est": sum(d["net"] for d in with_est) if with_est else None,
    }}


def deal_buy(conn, listing_id: str, buy_price: float, note: str = "") -> int:
    return deal_create(conn, listing_id, buy_price, note)


def deal_sell(conn, deal_id: int, sell_price: float, costs: float = 0.0) -> bool:
    cur = conn.execute(
        "UPDATE deals SET sell_price=?, sold_at=?, costs=COALESCE(costs,0)+? WHERE id=? AND sell_price IS NULL",
        (sell_price, time.time(), costs, deal_id),
    )
    conn.commit()
    return cur.rowcount == 1


def deal_stats(conn) -> dict:
    s = deals_list(conn)["summary"]
    return {"closed": s["closed"], "open": s["open"], "profit": s["net"],
            "avg_profit": s["net"] / s["closed"] if s["closed"] else 0.0,
            "roi": s["roi"] or 0.0, "avg_days": s["avg_days"] or 0.0}


# --- исключения и настройки --------------------------------------------------
def excluded(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"seller": set(), "model": set()}
    for kind, value in conn.execute("SELECT kind, value FROM excluded"):
        out.setdefault(kind, set()).add(value)
    return out


def exclude(conn, kind: str, value: str, title: str = "", on: bool = True) -> None:
    if kind not in ("seller", "model") or not value:
        raise ValueError("исключать можно продавца или модель")
    if on:
        conn.execute("INSERT OR REPLACE INTO excluded VALUES(?,?,?,?)", (kind, value, title or value, time.time()))
    else:
        conn.execute("DELETE FROM excluded WHERE kind=? AND value=?", (kind, value))
    conn.commit()


def get_setting(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()
