"""SQLite-хранилище: объявления, оповещения, сделки."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings(
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT '',
  url TEXT, title TEXT, description TEXT, city TEXT,
  price REAL, first_price REAL,
  first_seen REAL, last_seen REAL,
  status TEXT NOT NULL DEFAULT 'active',     -- active | gone
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
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def is_first_scan(conn, source: str) -> bool:
    return conn.execute("SELECT 1 FROM listings WHERE source=? LIMIT 1", (source,)).fetchone() is None


def upsert_listing(conn, l, source: str, now: float, baseline: bool = False) -> tuple[bool, bool]:
    """Возвращает (новое, цена_изменилась)."""
    row = conn.execute("SELECT price, status FROM listings WHERE id=?", (l.id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO listings(id,source,url,title,description,city,price,first_price,first_seen,last_seen,baseline)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (l.id, source, l.url, l.title, l.description, l.city, l.price, l.price, now, now, int(baseline)),
        )
        return True, False
    changed = row["price"] != l.price
    conn.execute(
        "UPDATE listings SET price=?, last_seen=?, status='active', gone_at=NULL, title=?, "
        "description=CASE WHEN ?<>'' THEN ? ELSE description END, source=? WHERE id=?",
        (l.price, now, l.title, l.description or "", l.description or "", source, l.id),
    )
    return False, changed


def mark_gone(conn, source: str, scan_start: float) -> int:
    cur = conn.execute(
        "UPDATE listings SET status='gone', gone_at=? WHERE source=? AND status='active' AND last_seen<?",
        (scan_start, source, scan_start),
    )
    return cur.rowcount


def save_parse(conn, listing_id: str, p) -> None:
    conn.execute(
        "UPDATE listings SET kind=?, skip_reason=?, model=?, vram=?, gpu_key=?, flags=? WHERE id=?",
        (p.kind, p.skip_reason, p.model, p.vram, p.key or None, json.dumps(p.flags), listing_id),
    )


def save_score(conn, listing_id: str, net: float | None, score_json: str) -> None:
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
                 active_window_days: float = 14, sold_window_days: float = 60) -> MarketStats:
    st = MarketStats(key=key)
    rows = conn.execute(
        "SELECT price FROM listings WHERE gpu_key=? AND status='active' AND id<>? AND last_seen>=? "
        "AND price>0 AND (flags IS NULL OR flags NOT LIKE '%prepay%')",
        (key, exclude_id or "", now - active_window_days * 86400),
    ).fetchall()
    prices = _trim(sorted(r["price"] for r in rows))
    st.n = len(prices)
    if prices:
        st.median_ask = statistics.median(prices)
        st.p25_ask = statistics.quantiles(prices, n=4)[0] if len(prices) >= 4 else prices[0]
    gone = conn.execute(
        "SELECT price, first_seen, gone_at, baseline FROM listings "
        "WHERE gpu_key=? AND status='gone' AND gone_at>=?",
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
