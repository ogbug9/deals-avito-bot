"""Конвейер: приём объявлений → нормализация → рынок → скоринг → отбор → оповещение."""
from __future__ import annotations

import json
import time

from . import db
from .config import Config
from .normalizer import parse_listing
from .notifier import Notifier, format_alert
from .scoring import score_listing


def process(conn, cfg: Config, listings, source: str, complete: bool,
            notifier: Notifier | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    scan_start = now
    stats = {"seen": len(listings), "new": 0, "gone": 0, "parsed_ok": 0, "scored": 0,
             "candidates": 0, "alerts": 0, "skip_reasons": {}}

    first_scan = db.is_first_scan(conn, source)
    # 1. сохраняем + нормализуем
    for l in listings:
        is_new, _ = db.upsert_listing(conn, l, source, now, baseline=first_scan)
        stats["new"] += is_new
        p = parse_listing(l.title, l.description)
        db.save_parse(conn, l.id, p)
        stats["parsed_ok"] += p.ok
        if not p.ok:
            r = p.skip_reason or p.kind
            stats["skip_reasons"][r] = stats["skip_reasons"].get(r, 0) + 1
    # 2. «ушедшие» — только после полного обхода
    if complete and listings:
        stats["gone"] = db.mark_gone(conn, source, scan_start)
    conn.commit()

    # 3. скоринг (рынок считаем уже по обновлённой базе)
    cands = []
    for l in listings:
        row = conn.execute("SELECT gpu_key, flags FROM listings WHERE id=?", (l.id,)).fetchone()
        if not row["gpu_key"]:
            continue
        flags = json.loads(row["flags"] or "{}")
        ms = db.market_stats(conn, row["gpu_key"], l.id, now)
        sc = score_listing(l.price, flags, ms, cfg)
        stats["scored"] += 1
        db.save_score(conn, l.id, sc.net if sc.ok else None, json.dumps(sc.to_dict(), ensure_ascii=False))
        if sc.ok:
            cands.append((sc.net, l, row["gpu_key"], sc))
    stats["candidates"] = len(cands)

    # 4. отбор и оповещение: лучшие по чистой прибыли, без повторов
    cands.sort(key=lambda x: -x[0])
    for net, l, key, sc in cands:
        if stats["alerts"] >= cfg.max_alerts_per_scan:
            break
        prev = db.last_alert(conn, l.id)
        if prev and l.price > prev["price"] * (1 - cfg.realert_drop_pct):
            continue
        info = parse_listing(l.title, l.description).info
        if notifier:
            notifier.send(format_alert(l.title, key, l.url, sc, info))
        db.record_alert(conn, l.id, l.price, net, now)
        stats["alerts"] += 1
    conn.commit()
    return stats
