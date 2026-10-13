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
SCHEMA_VERSION = 3


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


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        has_data = conn.execute("SELECT 1 FROM sqlite_master WHERE name='listings'").fetchone() is not None
        if has_data:
            saved = backup(path)
            if saved:
                print(f"База обновляется до версии {SCHEMA_VERSION}; копия: {saved}", flush=True)
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
        for col, decl in EXTRA_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {decl}")
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
    row = conn.execute("SELECT price, status FROM listings WHERE id=?", (l.id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO listings(id,source,url,title,description,city,price,first_price,first_seen,last_seen,baseline,"
            "category,region,delivery,image,date_text,rating,reviews,posted_at,weekday,is_msk) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (l.id, source, l.url, l.title, l.description, l.city, l.price, l.price, now, now, int(baseline),
             category, region, *extra, posted, weekday, msk),
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
        "weekday=COALESCE(weekday, ?), is_msk=MAX(is_msk, ?) WHERE id=?",
        (l.price, now, l.title, l.description or "", l.description or "", source, l.city or "", l.city or "",
         category, category, region, region, *extra, posted, weekday, msk, l.id),
    )
    return False, changed


def mark_gone(conn, source: str, scan_start: float) -> int:
    cur = conn.execute(
        "UPDATE listings SET status='inactive', gone_at=? WHERE source=? AND status='active' AND last_seen<?",
        (scan_start, source, scan_start),
    )
    return cur.rowcount


def save_parse(conn, listing_id: str, p, defect: bool = False, wanted: bool = False, part=None, urgent: bool = False) -> None:
    """p — normalizer.Parsed; part — parts.Part для не-видеокарт (позиция, количество, бренд)."""
    item_key = (part.key if part else p.key) or None
    qty = part.qty if part and part.qty else None
    conn.execute(
        "UPDATE listings SET kind=?, skip_reason=?, model=?, vram=?, gpu_key=?, flags=?, defect=?, wanted=?, "
        "item_key=?, qty=?, unit_price=CASE WHEN ? IS NOT NULL AND price>0 THEN price/? END, brand=?, model_source=?, "
        "urgent=? WHERE id=?",
        (p.kind, p.skip_reason, p.model, p.vram, p.key or None, json.dumps(p.flags), int(defect), int(wanted),
         item_key, qty, qty, qty, part.brand if part else None, getattr(p, "model_source", "") or None, int(urgent),
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
    sold_n: int = 0
    median_sold_price: float | None = None
    days_to_sell: float | None = None


def _trim(prices: list[float]) -> list[float]:
    if len(prices) < 4:
        return prices
    med = statistics.median(prices)
    kept = [p for p in prices if 0.5 * med <= p <= 1.8 * med]
    return kept or prices


def market_stats(conn, key: str, exclude_id: str | None, now: float,
                 active_window_days: float = 14, sold_window_days: float = 60, msk: bool | None = None) -> MarketStats:
    """msk=True — только московские объявления, False — только региональные, None — все."""
    st = MarketStats(key=key)
    group = "" if msk is None else f" AND is_msk={int(msk)}"
    rows = conn.execute(
        "SELECT price FROM listings WHERE item_key=? AND status='active' AND id<>? AND last_seen>=? "
        "AND price>0 AND defect=0 AND wanted=0 AND (flags IS NULL OR flags NOT LIKE '%prepay%')" + group,
        (key, exclude_id or "", now - active_window_days * 86400),
    ).fetchall()
    prices = _trim(sorted(r["price"] for r in rows))
    st.n = len(prices)
    if prices:
        st.median_ask = statistics.median(prices)
        st.p25_ask = statistics.quantiles(prices, n=4)[0] if len(prices) >= 4 else prices[0]
    gone = conn.execute(
        "SELECT price, first_seen, gone_at, baseline FROM listings "
        "WHERE item_key=? AND status='inactive' AND gone_at>=?",
        (key, now - sold_window_days * 86400),
    ).fetchall()
    st.sold_n = len(gone)
    if len(gone) >= 3:
        st.median_sold_price = statistics.median(r["price"] for r in gone)
    # возраст считаем только у объявлений, появившихся уже после первого обхода
    lived = [(r["gone_at"] - r["first_seen"]) / 86400 for r in gone if not r["baseline"]]
    if len(lived) >= 3:
        st.days_to_sell = max(0.5, statistics.median(lived))
    return st


def last_alert(conn, listing_id: str):
    return conn.execute(
        "SELECT price FROM alerts WHERE listing_id=? ORDER BY sent_at DESC LIMIT 1", (listing_id,)
    ).fetchone()


def record_alert(conn, listing_id: str, price: float, net: float, now: float | None = None) -> None:
    conn.execute("INSERT INTO alerts VALUES(?,?,?,?)", (listing_id, price, net, time.time() if now is None else now))


# --- сделки (ручной учёт, нужен для порога «20 реальных сделок») -------------
def deal_buy(conn, listing_id: str, buy_price: float, note: str = "") -> int:
    r = conn.execute("SELECT gpu_key FROM listings WHERE id=?", (listing_id,)).fetchone()
    cur = conn.execute(
        "INSERT INTO deals(listing_id,gpu_key,buy_price,bought_at,note) VALUES(?,?,?,?,?)",
        (listing_id, r["gpu_key"] if r else None, buy_price, time.time(), note),
    )
    return cur.lastrowid


def deal_sell(conn, deal_id: int, sell_price: float, costs: float = 0.0) -> bool:
    cur = conn.execute(
        "UPDATE deals SET sell_price=?, sold_at=?, costs=? WHERE id=? AND sell_price IS NULL",
        (sell_price, time.time(), costs, deal_id),
    )
    return cur.rowcount == 1


def deal_stats(conn) -> dict:
    rows = conn.execute("SELECT * FROM deals WHERE sell_price IS NOT NULL").fetchall()
    open_n = conn.execute("SELECT COUNT(*) c FROM deals WHERE sell_price IS NULL").fetchone()["c"]
    if not rows:
        return {"closed": 0, "open": open_n, "profit": 0.0, "avg_profit": 0.0, "roi": 0.0, "avg_days": 0.0}
    profits = [r["sell_price"] - r["buy_price"] - (r["costs"] or 0) for r in rows]
    invested = sum(r["buy_price"] for r in rows)
    days = [(r["sold_at"] - r["bought_at"]) / 86400 for r in rows]
    return {
        "closed": len(rows), "open": open_n, "profit": sum(profits),
        "avg_profit": sum(profits) / len(rows),
        "roi": sum(profits) / invested if invested else 0.0,
        "avg_days": sum(days) / len(days),
    }
