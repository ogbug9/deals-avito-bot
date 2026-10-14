"""Конвейер: приём объявлений → нормализация → рынок → скоринг → режимы → отбор → оповещение."""
from __future__ import annotations

import json
import statistics
import time

from . import analogs, db, modes, parts, pcparts
from .config import Config
from .normalizer import Parsed, condition, is_new, is_urgent, junk_reason, parse_listing, risk_flags
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
    junk = junk_reason(l.title, l.description, category)
    db.save_parse(conn, l.id, p, defect=defect, wanted=wanted, part=part, urgent=urgent, junk=junk,
                  cond="new" if is_new(l.title, l.description) else "used")
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
    hidden = db.excluded(conn)
    caps = analogs.ceilings(conn, now, cfg.min_market_n) if cfg.analog_ceiling else {}
    pc_cache: dict = {}
    for l in listings:
        _mark_reseller(conn, cfg, l.id)
        row = conn.execute("SELECT * FROM listings WHERE id=?", (l.id,)).fetchone()
        sc = ms = None
        if row["item_key"]:
            keys.add(row["item_key"])
            ms = db.market_stats(conn, row["item_key"], l.id, now)
            if row["fine_key"]:           # точная позиция (с брендом) — если в ней хватает объявлений для алерта
                fine = db.market_stats(conn, row["fine_key"], l.id, now, column="fine_key")
                if fine.n >= cfg.alert_market_n:
                    ms = fine
            sc = score_listing(l.price, json.loads(row["flags"] or "{}"), ms, cfg, caps.get(row["item_key"]))
            sc.precise = ms.key == row["fine_key"]
            stats["scored"] += 1
            if (not row["junk"] and ms.n >= cfg.min_market_n and 0 < l.price < ms.median_ask * cfg.placeholder_ratio):
                # цена в разы ниже рынка — заглушка («цену уточняйте», лот из нескольких вещей): в мусор, из рынка долой
                conn.execute("UPDATE listings SET junk='price' WHERE id=?", (l.id,))
                row = conn.execute("SELECT * FROM listings WHERE id=?", (l.id,)).fetchone()
            db.save_score(conn, l.id, sc.net if sc.ok else None, json.dumps(sc.to_dict(), ensure_ascii=False))
        else:
            db.save_score(conn, l.id, None, None)   # иначе в report висит старая оценка уже отсеянного объявления
        if row["category"] == "pc":       # внутренняя стоимость: какие детали названы и сколько они стоят
            pcparts.store(conn, cfg, row, now, caps, pc_cache)
        cands = modes.evaluate(conn, cfg, row, sc, ms, now, log, caps)
        for c in cands:
            log.hit(c.mode)
        modes.store(conn, row, cands, now)
        alertable = [c for c in cands if c.mode in modes.ALERT_MODES]
        muted = (row["seller_id"] and row["seller_id"] in hidden["seller"]) or (row["item_key"] in hidden["model"])
        if alertable and not muted:       # скрытые продавцы и модели в алерты не идут
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
            fresh = conn.execute("SELECT * FROM listings WHERE id=?", (row["id"],)).fetchone()   # уже с оценкой рынка
            notifier.send(modes.format_alert(fresh, cands))
        if notifier or record_alerts:
            db.record_alert(conn, row["id"], row["price"], net, now)
            stats["alerts"] += 1
    _notify_saved_searches(conn, [l.id for l in listings], now, notifier)
    conn.commit()
    return stats


def _mark_reseller(conn, cfg: Config, lid: str) -> None:
    """Продавец с несколькими одинаковыми лотами (или дубли объявления) — флаг и штраф риска."""
    r = conn.execute("SELECT seller_id, item_key, title, description, price, flags, reseller FROM listings WHERE id=?",
                     (lid,)).fetchone()
    same = total = 0
    if r["seller_id"]:
        total = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active' AND seller_id=?", (r["seller_id"],)).fetchone()[0]
        if r["item_key"]:
            same = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active' AND seller_id=? AND item_key=?",
                                (r["seller_id"], r["item_key"])).fetchone()[0]
    dups = 0
    if len(r["description"] or "") >= 20:      # дубли: тот же заголовок, цена и текст (одного заголовка мало — он бывает типовым)
        dups = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active' AND title=? AND price=? AND description=?",
                            (r["title"], r["price"], r["description"])).fetchone()[0]
    reseller = int(same >= 3 or total >= 8 or dups >= 3)
    flags = json.loads(r["flags"] or "{}")
    if reseller:
        flags["reseller"] = cfg.reseller_penalty
    else:
        flags.pop("reseller", None)
    conn.execute("UPDATE listings SET reseller=?, seller_ads=?, flags=? WHERE id=?",
                 (reseller, total or None, json.dumps(flags), lid))


def _notify_saved_searches(conn, ids: list[str], now: float, notifier) -> None:
    """Сохранённые поиски с уведомлением: сообщить о новых объявлениях, которые под них подходят."""
    if not notifier or not ids:
        return
    from .query import build_query
    from urllib.parse import parse_qs
    fresh = [i for i in ids if conn.execute("SELECT first_seen FROM listings WHERE id=?", (i,)).fetchone()[0] == now]
    if not fresh:
        return
    for s in conn.execute("SELECT id, name, query FROM saved_searches WHERE notify=1").fetchall():
        where, args = build_query(parse_qs(s["query"] or ""), conn)
        marks = ",".join("?" * len(fresh))
        rows = conn.execute(f"SELECT title, price, url FROM listings WHERE {where} AND id IN ({marks}) ORDER BY price LIMIT 5",
                            args + fresh).fetchall()
        if rows:
            lines = [f"🔎 Сохранённый поиск «{s['name']}»: новых {len(rows)}"]
            lines += [f"• {r['title'][:70]} — {r['price']:,.0f} ₽".replace(",", " ") for r in rows]
            lines.append(rows[0]["url"] or "")
            notifier.send("\n".join(lines))
            conn.execute("UPDATE saved_searches SET notified_at=? WHERE id=?", (now, s["id"]))


def rescore(conn, cfg: Config, now: float | None = None) -> dict:
    """Пересчитать всю базу текущими правилами (после правки words/*.txt, порогов или кода). Сеть не нужна.

    Два прохода: сначала позиции, потом оценки — чтобы медианы считались по уже обновлённым позициям.
    Алерты не шлются, счётчики режимов не пишутся.
    """
    from . import progress
    from .collector import RawListing
    now = time.time() if now is None else now
    progress.start("analysis", "Анализ базы: пересчёт оценок, разбор ПК, линейки")
    groups: dict[tuple, list] = {}
    for r in conn.execute("SELECT * FROM listings WHERE status='active'"):
        l = RawListing(id=r["id"], title=r["title"] or "", price=r["price"] or 0.0, url=r["url"] or "",
                       description=r["description"] or "", city=r["city"] or "", delivery=bool(r["delivery"]),
                       image=r["image"] or "", rating=r["rating"], reviews=r["reviews"],
                       seller_id=r["seller_id"] or "", seller_name=r["seller_name"] or "")
        groups.setdefault((r["source"], r["category"], r["region"]), []).append(l)
    total = {"listings": 0, "positions": 0, "candidates": 0, "modes": {}, "gone": 0}
    for final in (False, True):
        for (source, category, region), items in groups.items():
            for l in items:        # last_seen не трогаем: пересчёт не означает, что объявление снова видели
                seen = conn.execute("SELECT last_seen FROM listings WHERE id=?", (l.id,)).fetchone()[0]
                l._last_seen = seen
            progress.set(source=f"{'оценки' if final else 'позиции'}: {category or 'без раздела'}")
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
    total["gone"] = analyze_gone(conn, cfg, now)
    conn.commit()
    progress.done(f"пересчитано {total['listings']}, кандидатов {total['candidates']}")
    return total


def analyze_gone(conn, cfg: Config, now: float) -> int:
    """Аналитика для ушедших объявлений: позиция, мусор, оценка и разбор ПК — теми же правилами, что и для активных.

    Статус, даты и история не меняются; кандидатами ушедшие не становятся. Нужна, чтобы по всей базе
    (а не только по тому, что сейчас в продаже) работали фильтры, сортировки и сравнения.
    """
    from .collector import RawListing
    caps = analogs.ceilings(conn, now, cfg.min_market_n) if cfg.analog_ceiling else {}
    cache: dict = {}
    rows = conn.execute("SELECT * FROM listings WHERE status<>'active'").fetchall()
    for r in rows:
        l = RawListing(id=r["id"], title=r["title"] or "", price=r["price"] or 0.0, url=r["url"] or "",
                       description=r["description"] or "", city=r["city"] or "")
        _normalize(conn, l, r["category"])
        row = conn.execute("SELECT * FROM listings WHERE id=?", (r["id"],)).fetchone()
        if row["item_key"]:
            ms = db.market_stats(conn, row["item_key"], row["id"], now)
            sc = score_listing(row["price"] or 0.0, json.loads(row["flags"] or "{}"), ms, cfg, caps.get(row["item_key"]))
            db.save_score(conn, row["id"], sc.net if sc.ok else None, json.dumps(sc.to_dict(), ensure_ascii=False))
        else:
            db.save_score(conn, row["id"], None, None)
        if row["category"] == "pc":
            pcparts.store(conn, cfg, row, now, caps, cache)
    return len(rows)


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


def deep_targets(conn, cfg: Config, now: float, limit: int) -> list:
    """Кого читать целиком: лоты, прошедшие предфильтр по цене (заметно ниже медианы позиции, в бюджете), и
    системные блоки в бюджете (в выдаче их описание обрезано). Уже прочитанные не трогаем, пока цена не изменилась."""
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT l.id, l.url FROM listings l WHERE l.status='active' AND l.url<>'' AND l.defect=0 AND l.wanted=0 "
        "AND l.price BETWEEN ? AND ? AND (l.deep_at IS NULL OR EXISTS("
        "  SELECT 1 FROM price_history h WHERE h.listing_id=l.id AND h.ts>l.deep_at)) AND ("
        "  (json_extract(l.score_json, '$.market_n')>=? AND l.price<=json_extract(l.score_json, '$.median_ask')*?)"
        "  OR l.kind='pc') "
        "ORDER BY (l.mode_tags IS NULL), COALESCE(json_extract(l.score_json, '$.discount_vs_market'), 0) DESC, l.first_seen DESC "
        "LIMIT ?",
        (cfg.min_price, cfg.max_price, cfg.min_market_n, 1 - cfg.deep_discount, limit)).fetchall()
    return list(rows) + pc_deep_targets(conn, cfg, cfg.pc_deep_per_cycle, {r["id"] for r in rows})


def pc_deep_targets(conn, cfg: Config, limit: int, skip: set) -> list:
    """Системные блоки для чтения целиком: в выдаче описание обрезано, и без карточки детали не разобрать.
    Сначала те, что дешевле похожих ПК и у которых оценено меньше половины деталей."""
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT l.id, l.url FROM listings l WHERE l.status='active' AND l.url<>'' AND l.category='pc' AND l.defect=0 "
        "AND l.wanted=0 AND l.junk IS NULL AND l.deep_at IS NULL AND l.price BETWEEN ? AND ? AND l.parts_json IS NOT NULL "
        "AND COALESCE(json_extract(l.parts_json, '$.priced'), 0) * 2 < COALESCE(json_extract(l.parts_json, '$.expected'), 6) "
        "ORDER BY COALESCE(json_extract(l.parts_json, '$.peers.diff_pct'), 0) DESC, l.first_seen DESC LIMIT ?",
        (cfg.min_price, cfg.max_price, limit + len(skip))).fetchall()
    return [r for r in rows if r["id"] not in skip][:limit]


def recheck(conn, cfg: Config, check, now: float | None = None, limit: int | None = None,
            deep_limit: int | None = None, notifier: Notifier | None = None) -> dict:
    """Открывает карточки: (1) глубокое чтение лотов, прошедших предфильтр, (2) перепроверка кандидатов,
    (3) одна случайная из остальных — для оценки срока жизни объявлений.

    check(url) → {"state": "active" | "inactive" | "unknown", "description", "condition", "city", "photos", …}.
    Пропавшие объявления становятся inactive и выходят из медианы. Прочитанное кэшируется (deep_at).
    """
    now = time.time() if now is None else now
    limit = cfg.recheck_per_cycle if limit is None else limit
    deep_limit = cfg.deep_per_cycle if deep_limit is None else deep_limit
    out = {"checked": 0, "inactive": 0, "unknown": 0, "deep": 0}
    todo: dict[str, tuple[str, bool]] = {r["id"]: (r["url"], True) for r in deep_targets(conn, cfg, now, deep_limit)}
    # (deep_targets уже добавил до cfg.pc_deep_per_cycle системных блоков сверх общего лимита)
    if limit > 0:
        cands = conn.execute(
            "SELECT DISTINCT l.id, l.url FROM listings l JOIN candidates c ON c.listing_id=l.id "
            "WHERE l.status='active' AND l.url<>'' AND COALESCE(l.checked_at,0)<? ORDER BY COALESCE(l.checked_at,0), c.est_profit DESC LIMIT ?",
            (now - 6 * 3600, max(1, limit - 1))).fetchall()
        for r in list(cands)[:limit]:
            todo.setdefault(r["id"], (r["url"], False))
    # Давно не виденные: объявление ушло вглубь выдачи или снято? Проверяем понемногу, начиная с самых «старых»,
    # сначала те, у которых определена позиция, — они нужны для срока продажи и цен ушедших лотов.
    if cfg.verify_per_cycle > 0 and (limit > 0 or deep_limit > 0):
        stale = conn.execute(
            "SELECT id, url FROM listings WHERE status='active' AND url<>'' AND last_seen<? AND COALESCE(checked_at,0)<? "
            "ORDER BY (item_key IS NULL), last_seen, id LIMIT ?",
            (now - cfg.stale_days * 86400, now - cfg.stale_days * 86400, cfg.verify_per_cycle)).fetchall()
        for r in stale:
            todo.setdefault(r["id"], (r["url"], False))
    changed: list[str] = []
    for lid, (url, deep) in todo.items():
        res = check(url)
        out["checked"] += 1
        if res["state"] == "unknown":
            out["unknown"] += 1
            continue                # капча или непонятная страница — статус не трогаем
        conn.execute("UPDATE listings SET checked_at=? WHERE id=?", (now, lid))
        conn.execute("UPDATE candidates SET checked_at=? WHERE listing_id=?", (now, lid))
        if res["state"] == "inactive":
            out["inactive"] += 1
            conn.execute("UPDATE listings SET status='inactive', gone_at=? WHERE id=?", (now, lid))
            conn.execute("UPDATE candidates SET inactive_at=COALESCE(inactive_at, ?) WHERE listing_id=?", (now, lid))
            continue
        conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (now, lid))   # карточка открылась — объявление живо
        _store_card(conn, lid, res, now)
        out["deep"] += 1
        changed.append(lid)
    conn.commit()
    if changed:                     # полное описание могло раскрыть брак или детали ПК — пересчитываем эти лоты
        _reprocess(conn, cfg, changed, now, notifier)
    return out


def _store_card(conn, lid: str, res: dict, now: float) -> None:
    sets = {"deep_at": now}
    for src, col in (("condition", "avito_condition"), ("seller_name", "seller_name"), ("seller_type", "seller_type"),
                     ("avito_model", "avito_model"), ("rating", "rating")):
        if res.get(src):
            sets[col] = res[src]
    if res.get("photos"):
        sets["photos"] = json.dumps(res["photos"])
    conn.execute(f"UPDATE listings SET {', '.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), lid])
    if res.get("city"):
        conn.execute("UPDATE listings SET city=?, is_msk=MAX(is_msk, ?) WHERE id=? AND COALESCE(city,'')=''",
                     (res["city"], int(db.is_moscow("", res["city"])), lid))
    if res.get("description"):
        conn.execute("UPDATE listings SET description=? WHERE id=? AND length(?)>length(COALESCE(description,''))",
                     (res["description"], lid, res["description"]))


def _reprocess(conn, cfg: Config, ids: list[str], now: float, notifier: Notifier | None) -> None:
    """Пересчитать отдельные объявления по сохранённым данным (после чтения карточки). last_seen не меняется."""
    from .collector import RawListing
    groups: dict[tuple, list] = {}
    for lid in ids:
        r = conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        l = RawListing(id=r["id"], title=r["title"] or "", price=r["price"] or 0.0, url=r["url"] or "",
                       description=r["description"] or "", city=r["city"] or "", delivery=bool(r["delivery"]),
                       image=r["image"] or "", rating=r["rating"], reviews=r["reviews"],
                       seller_id=r["seller_id"] or "", seller_name=r["seller_name"] or "")
        l._last_seen = r["last_seen"]
        groups.setdefault((r["source"], r["category"], r["region"]), []).append(l)
    for (source, category, region), items in groups.items():
        process(conn, cfg, items, source, False, notifier=notifier, now=now, category=category, region=region,
                save_stats=False)
        for l in items:
            conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (l._last_seen, l.id))
    conn.commit()
