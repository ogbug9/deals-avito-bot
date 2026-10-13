"""Конвейер: приём объявлений → нормализация → рынок → скоринг → режимы → отбор → оповещение."""
from __future__ import annotations

import json
import random
import statistics
import time

from . import db, modes, parts
from .config import Config
from .normalizer import Parsed, condition, is_urgent, parse_listing, risk_flags
from .notifier import Notifier
from .scoring import score_listing

GPU_CATEGORIES = modes.GPU_CATEGORIES   # '' — старые записи и CSV: считаем видеокартами


def _normalize(conn, l, category: str) -> Parsed:
    """Разбирает объявление по правилам его раздела и сохраняет результат в строку listings."""
    defect, wanted = condition(l.title, l.description)
    text = f"{l.title} {l.description}"
    urgent, part = is_urgent(text), None
    if category in GPU_CATEGORIES:
        p = parse_listing(l.title, l.description, assume_gpu=category == "videokarty")
    elif category == "pc":
        p = Parsed(kind="pc", skip_reason="defective" if defect else "wanted" if wanted else "bundle",
                   flags=risk_flags(text))
    else:
        part = None if (defect or wanted) else parts.parse_part(category, l.title, l.description)
        p = Parsed(kind=category, flags=risk_flags(text),
                   skip_reason="defective" if defect else "wanted" if wanted else "" if part else "position_not_recognized")
    db.save_parse(conn, l.id, p, defect=defect, wanted=wanted, part=part, urgent=urgent)
    p.item_key = part.key if part else p.key
    return p


def process(conn, cfg: Config, listings, source: str, complete: bool,
            notifier: Notifier | None = None, now: float | None = None,
            category: str = "", region: str = "", record_alerts: bool = True, save_stats: bool = True) -> dict:
    now = time.time() if now is None else now
    scan_start = now
    stats = {"seen": len(listings), "new": 0, "gone": 0, "parsed_ok": 0, "scored": 0,
             "candidates": 0, "alerts": 0, "skip_reasons": {}}

    first_scan = db.is_first_scan(conn, source)
    # 1. сохраняем + нормализуем
    for l in listings:
        is_new, _ = db.upsert_listing(conn, l, source, now, baseline=first_scan, category=category, region=region)
        stats["new"] += is_new
        p = _normalize(conn, l, category)
        stats["parsed_ok"] += bool(p.item_key)
        if not p.item_key:
            r = p.skip_reason or p.kind
            stats["skip_reasons"][r] = stats["skip_reasons"].get(r, 0) + 1
    # 2. «ушедшие» — только после полного обхода
    if complete and listings:
        stats["gone"] = db.mark_gone(conn, source, scan_start)
    conn.commit()

    # 3. скоринг и режимы (рынок считаем уже по обновлённой базе)
    log = modes.ModeLog()
    found = []
    keys = set()
    for l in listings:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (l.id,)).fetchone()
        sc = ms = None
        if row["item_key"]:
            keys.add(row["item_key"])
            ms = db.market_stats(conn, row["item_key"], l.id, now)
            sc = score_listing(l.price, json.loads(row["flags"] or "{}"), ms, cfg)
            stats["scored"] += 1
            db.save_score(conn, l.id, sc.net if sc.ok else None, json.dumps(sc.to_dict(), ensure_ascii=False))
        else:
            db.save_score(conn, l.id, None, None)   # иначе в report висит старая оценка уже отсеянного объявления
        cands = modes.evaluate(conn, cfg, row, sc, ms, now, log)
        for c in cands:
            log.hit(c.mode)
        modes.store(conn, row, cands, now)
        alertable = [c for c in cands if c.mode in modes.ALERT_MODES]
        if alertable:
            found.append((max(c.est_profit for c in alertable), row, alertable))
    stats["candidates"] = len(found)
    stats["modes"] = log.summary()
    if save_stats:
        log.save(conn, source, now)
    _save_market_history(conn, keys, now)

    # 4. отбор и оповещение: лучшие по чистой прибыли, без повторов
    found.sort(key=lambda x: -x[0])
    for net, row, cands in found:
        if stats["alerts"] >= cfg.max_alerts_per_scan:
            break
        prev = db.last_alert(conn, row["id"])
        if prev and row["price"] > prev["price"] * (1 - cfg.realert_drop_pct):
            continue
        if notifier:
            notifier.send(modes.format_alert(row, cands))
        if notifier or record_alerts:
            db.record_alert(conn, row["id"], row["price"], net, now)
            stats["alerts"] += 1
    conn.commit()
    return stats


def rescore(conn, cfg: Config, now: float | None = None) -> dict:
    """Пересчитать всю базу текущими правилами (после правки words/*.txt, порогов или кода). Сеть не нужна.

    Два прохода: сначала позиции, потом оценки — чтобы медианы считались по уже обновлённым позициям.
    Алерты не шлются, счётчики режимов не пишутся.
    """
    from .collector import RawListing
    now = time.time() if now is None else now
    groups: dict[tuple, list] = {}
    for r in conn.execute("SELECT * FROM listings WHERE status='active'"):
        l = RawListing(id=r["id"], title=r["title"] or "", price=r["price"] or 0.0, url=r["url"] or "",
                       description=r["description"] or "", city=r["city"] or "", delivery=bool(r["delivery"]),
                       image=r["image"] or "", rating=r["rating"], reviews=r["reviews"])
        groups.setdefault((r["source"], r["category"], r["region"]), []).append(l)
    total = {"listings": 0, "positions": 0, "candidates": 0, "modes": {}}
    for final in (False, True):
        for (source, category, region), items in groups.items():
            for l in items:        # last_seen не трогаем: пересчёт не означает, что объявление снова видели
                seen = conn.execute("SELECT last_seen FROM listings WHERE id=?", (l.id,)).fetchone()[0]
                l._last_seen = seen
            st = process(conn, cfg, items, source, False, now=now, category=category, region=region,
                         record_alerts=False, save_stats=False)
            for l in items:
                conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (l._last_seen, l.id))
            if final:
                total["listings"] += st["seen"]
                total["positions"] += st["parsed_ok"]
                total["candidates"] += st["candidates"]
                for m, d in st["modes"].items():
                    t = total["modes"].setdefault(m, {"viewed": 0, "candidates": 0, "rejected": {}})
                    t["viewed"] += d["viewed"]
                    t["candidates"] += d["candidates"]
                    for k, v in d["rejected"].items():
                        t["rejected"][k] = t["rejected"].get(k, 0) + v
    conn.commit()
    return total


def _save_market_history(conn, keys: set[str], now: float) -> None:
    """Медиана и объём по позиции за день — для вкладки «Рынок»."""
    if now < 86400:
        return                      # тестовые «нулевые» времена
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    for key in keys:
        prices = [r[0] for r in conn.execute(
            "SELECT price FROM listings WHERE item_key=? AND status='active' AND price>0 AND defect=0 AND wanted=0 "
            "AND last_seen>=?", (key, now - 14 * 86400))]
        if prices:
            conn.execute("INSERT OR REPLACE INTO market_history VALUES(?,?,?,?)",
                         (day, key, statistics.median(prices), len(prices)))


def recheck(conn, cfg: Config, check, now: float | None = None, limit: int | None = None) -> dict:
    """Перепроверка карточек: кандидаты (давно не проверенные) и одна случайная из остальных.

    check(url) → {"state": "active" | "inactive" | "unknown", "description": str, "condition": str}.
    Пропавшие объявления становятся inactive и выходят из медианы.
    """
    now = time.time() if now is None else now
    limit = cfg.recheck_per_cycle if limit is None else limit
    out = {"checked": 0, "inactive": 0, "unknown": 0}
    if limit <= 0:
        return out
    rows = conn.execute(
        "SELECT DISTINCT l.id, l.url FROM listings l JOIN candidates c ON c.listing_id=l.id "
        "WHERE l.status='active' AND l.url<>'' AND COALESCE(l.checked_at,0)<? ORDER BY COALESCE(l.checked_at,0), c.est_profit DESC LIMIT ?",
        (now - 6 * 3600, max(1, limit - 1))).fetchall()
    others = conn.execute(
        "SELECT id, url FROM listings WHERE status='active' AND url<>'' AND item_key IS NOT NULL AND last_seen<? "
        "AND COALESCE(checked_at,0)<? AND id NOT IN (SELECT listing_id FROM candidates)",
        (now - 2 * 86400, now - 2 * 86400)).fetchall()
    rows = list(rows) + (random.sample(others, 1) if others and len(rows) < limit else [])
    for r in rows[:limit]:
        res = check(r["url"])
        out["checked"] += 1
        if res["state"] == "unknown":
            out["unknown"] += 1
            continue                # капча или непонятная страница — статус не трогаем
        conn.execute("UPDATE listings SET checked_at=? WHERE id=?", (now, r["id"]))
        conn.execute("UPDATE candidates SET checked_at=? WHERE listing_id=?", (now, r["id"]))
        if res["state"] == "inactive":
            out["inactive"] += 1
            conn.execute("UPDATE listings SET status='inactive', gone_at=? WHERE id=?", (now, r["id"]))
            conn.execute("UPDATE candidates SET inactive_at=COALESCE(inactive_at, ?) WHERE listing_id=?", (now, r["id"]))
        else:
            if res.get("condition"):
                conn.execute("UPDATE listings SET avito_condition=? WHERE id=?", (res["condition"], r["id"]))
            if res.get("city"):
                conn.execute("UPDATE listings SET city=?, is_msk=MAX(is_msk, ?) WHERE id=? AND COALESCE(city,'')=''",
                             (res["city"], int(db.is_moscow("", res["city"])), r["id"]))
            if len(res.get("description") or "") > 0:
                conn.execute("UPDATE listings SET description=? WHERE id=? AND length(?)>length(COALESCE(description,''))",
                             (res["description"], r["id"], res["description"]))
    conn.commit()
    return out
