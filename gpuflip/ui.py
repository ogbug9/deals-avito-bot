"""Локальный интерфейс: объявления, сравнение, рынок, сделки, статус сбора, настройки.

Только стандартная библиотека. Слушает 127.0.0.1 — снаружи не виден.
"""
from __future__ import annotations

import csv
import io
import json
import statistics
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, analogs, compare, db, modes, sources
from .config import Config, set_env
from .normalizer import JUNK_RU
from .notifier import Notifier
from .paths import home, package_dir
from .query import FILTER_LABELS, STALE_DAYS, USER_STATUSES, _int, build_query, order_by
from .scoring import price_points

WEB_DIR = package_dir() / "web"
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
SCAN_LOG = "scan.log"
MIN_SCAN_GAP = 600          # не чаще раза в 10 минут: частые обходы = капча Авито
FIELDS = ("id, url, title, description, city, price, first_price, category, region, delivery, image, date_text, "
          "posted_at, first_seen, last_seen, rating, reviews, defect, wanted, gpu_key, item_key, fine_key, unit_price, qty, "
          "flags, net_profit, score_json, status, mode_tags, est_profit, est_profit_pct, reason, confidence, model_source, "
          "urgent, is_msk, avito_condition, fav, user_status, note, seller_id, seller_name, seller_type, seller_ads, "
          "reseller, photos, deep_at, avito_model, checklist, brand, gone_at, checked_at, junk, cond, parts_json, parts_value")
# чек-лист проверки на месте: ключ → подпись (отметки хранятся в записи лота)
CHECKLIST = [
    ("stress", "Нагрузка FurMark / OCCT 10–15 минут без вылетов"),
    ("temps", "Температуры под нагрузкой в норме, кулеры крутятся"),
    ("artifacts", "Нет артефактов, полос и мерцания"),
    ("serial", "Серийный номер на плате совпадает с коробкой / наклейкой"),
    ("seals", "Пломбы на винтах целы, следов вскрытия и ремонта нет"),
    ("ports", "Все видеовыходы дают картинку"),
    ("look", "Нет прогаров, окислов, погнутой планки и сколов текстолита"),
]
CONFIG_KEYS = {  # ключ .env → (поле Config, минимум, максимум)
    "LIGHT_MIN_SEC": ("light_min_sec", 60, 3600), "LIGHT_MAX_SEC": ("light_max_sec", 60, 7200),
    "FULL_INTERVAL_SEC": ("full_interval_sec", 600, 86400), "MAX_REQUESTS_PER_HOUR": ("max_requests_per_hour", 5, 200),
}
PREF_KEYS = ("ui.view", "ui.profiles", "ui.compare")


def _row(r, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    d = {k: r[k] for k in r.keys() if k not in ("score_json", "flags", "photos", "checklist", "parts_json")}
    d["parts"] = json.loads(r["parts_json"]) if r["parts_json"] else None
    d["flags"] = json.loads(r["flags"] or "{}")
    sc = json.loads(r["score_json"]) if r["score_json"] else None
    d["score"] = sc and {k: sc.get(k) for k in ("ok", "reason", "median_ask", "market_n", "net", "net_pct", "target_buy",
                                                 "sell_price", "risk_loss", "discount_vs_market", "low_data", "ask_net",
                                                 "position", "precise", "days_to_sell", "days_real", "days_n",
                                                 "quick_price", "comfort_price", "comfort_net", "p25", "p10", "sold_median",
                                                 "sold_n", "ceiling", "ceiling_key", "used_only", "new_n", "basis", "why",
                                                 "profit")}
    d["category_title"] = sources.CATEGORIES.get(r["category"], (r["category"] or "—",))[0]
    d["junk_title"] = JUNK_RU.get(r["junk"], r["junk"]) if r["junk"] else ""
    d["modes"] = [{"key": m, "title": modes.MODE_TITLES.get(m, m)} for m in (r["mode_tags"] or "").split(",") if m]
    d["photos"] = json.loads(r["photos"]) if r["photos"] else ([r["image"]] if r["image"] else [])
    d["checklist"] = json.loads(r["checklist"]) if r["checklist"] else {}
    # сигналы по истории цены
    drop = (r["first_price"] - r["price"]) if r["first_price"] and r["price"] and r["first_price"] > r["price"] else 0
    d["price_drop"] = drop
    d["price_drop_pct"] = drop / r["first_price"] if drop else 0
    start = r["posted_at"] or r["first_seen"]
    d["days_listed"] = (((r["gone_at"] if r["status"] == "inactive" and r["gone_at"] else now) - start) / 86400) if start else None
    d["age_hours"] = (now - start) / 3600 if start else None
    # жизнь объявления: когда впервые увидели, когда видели в последний раз, когда ушло и сколько прожило
    gone = r["status"] == "inactive"
    d["gone"] = gone
    d["lifetime_days"] = ((r["gone_at"] - start) / 86400) if gone and r["gone_at"] and start else None
    d["unseen_days"] = (now - r["last_seen"]) / 86400 if r["last_seen"] and not gone else None
    d["stale"] = bool(not gone and d["unseen_days"] is not None and d["unseen_days"] >= STALE_DAYS)
    return d


def version_info() -> dict:
    """Версия из пакета и короткий хеш коммита, если запущено из git-папки."""
    project = Path(__file__).resolve().parent.parent
    commit = ""
    if (project / ".git").exists():
        try:
            commit = subprocess.run(["git", "-C", str(project), "rev-parse", "--short", "HEAD"], capture_output=True,
                                    text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"version": __version__, "commit": commit, "frozen": bool(getattr(sys, "frozen", False))}


class App:
    def __init__(self, db_path: str, sources_path: str = "sources.json", desktop=None, env_path: str = ".env"):
        self.db_path, self.sources_path, self.desktop, self.env_path = db_path, sources_path, desktop, env_path
        self.scan_proc: subprocess.Popen | None = None
        self.scan_started = 0.0
        self._version: dict | None = None

    def conn(self):
        return db.connect(self.db_path)

    # ---------------- объявления ----------------
    def listings(self, q: dict) -> dict:
        c = self.conn()
        try:
            where, args = build_query(q, c)
            if "offset" in q:                      # окно для виртуального списка
                per = min(300, max(1, _int(q, "limit", 100)))
                offset = max(0, _int(q, "offset"))
                page = offset // per + 1
            else:
                per = min(100, max(1, _int(q, "per", 40)))
                page = max(1, _int(q, "page", 1))
                offset = (page - 1) * per
            total, pmin, pmax = c.execute(f"SELECT COUNT(*), MIN(price), MAX(price) FROM listings WHERE {where}", args).fetchone()
            rows = c.execute(f"SELECT {FIELDS} FROM listings WHERE {where} ORDER BY {order_by(q)}, id LIMIT ? OFFSET ?",
                             args + [per, offset]).fetchall()
            now = time.time()
            out = {"total": total, "page": page, "per": per, "offset": offset, "price_min": pmin, "price_max": pmax,
                   "items": [_row(r, now) for r in rows]}
            if not total:
                out["hints"] = self._empty_hints(c, q)
            return out
        finally:
            c.close()

    def _empty_hints(self, c, q: dict) -> list[dict]:
        """Пустая выдача: какой фильтр убрать, чтобы что-то появилось (и сколько тогда найдётся)."""
        hints = []
        for key, label in FILTER_LABELS.items():
            if not (q.get(key, [""])[0] or "").strip():
                continue
            where, args = build_query({k: v for k, v in q.items() if k != key}, c)
            n = c.execute(f"SELECT COUNT(*) FROM listings WHERE {where}", args).fetchone()[0]
            if n:
                hints.append({"param": key, "label": label, "count": n})
        return sorted(hints, key=lambda h: -h["count"])[:4]

    def listing(self, lid: str) -> dict:
        c = self.conn()
        try:
            r = c.execute(f"SELECT {FIELDS} FROM listings WHERE id=?", (lid,)).fetchone()
            if not r:
                raise ValueError("объявление не найдено")
            now = time.time()
            d = _row(r, now)
            d["price_history"] = [{"ts": ts, "price": p} for ts, p in c.execute(
                "SELECT ts, price FROM price_history WHERE listing_id=? ORDER BY ts", (lid,))]
            d["market_history"] = [{"day": day, "median": m, "n": n} for day, m, n in c.execute(
                "SELECT day, median, n FROM market_history WHERE key=? ORDER BY day", (r["item_key"] or "",))]
            d["candidates"] = [dict(x) for x in c.execute(
                "SELECT mode, est_profit, est_profit_pct, reason, confidence, first_at, checked_at, inactive_at "
                "FROM candidates WHERE listing_id=? ORDER BY est_profit DESC", (lid,))]
            for x in d["candidates"]:
                x["title"] = modes.MODE_TITLES.get(x["mode"], x["mode"])
            d["similar"] = analogs.similar(c, lid, now)
            d["equivalents"] = analogs.cheaper_equivalents(c, r["item_key"], now) if r["item_key"] else None
            d["liquidity"] = db.liquidity(c, r["item_key"], now) if r["item_key"] else None
            d["checklist_items"] = [{"key": k, "label": label} for k, label in CHECKLIST]
            hidden = db.excluded(c)
            d["seller_hidden"] = bool(r["seller_id"] and r["seller_id"] in hidden["seller"])
            d["model_hidden"] = bool(r["item_key"] and r["item_key"] in hidden["model"])
            return d
        finally:
            c.close()

    def update_listing(self, data: dict) -> dict:
        lid = str(data.get("id") or "")
        sets, args = [], []
        if "fav" in data:
            sets.append("fav=?")
            args.append(int(bool(data["fav"])))
        if "user_status" in data:
            st = data["user_status"] or None
            if st is not None and st not in USER_STATUSES:
                raise ValueError("неизвестный статус")
            sets.append("user_status=?")
            args.append(st)
        if "note" in data:
            sets.append("note=?")
            args.append((str(data["note"] or "")[:2000]) or None)
        if "checklist" in data:
            known = {k for k, _ in CHECKLIST}
            marks = {k: bool(v) for k, v in (data["checklist"] or {}).items() if k in known and v}
            sets.append("checklist=?")
            args.append(json.dumps(marks) if marks else None)
        if not lid or not sets:
            raise ValueError("нечего менять")
        c = self.conn()
        try:
            n = c.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id=?", args + [lid]).rowcount
            c.commit()
            if not n:
                raise ValueError("объявление не найдено")
        finally:
            c.close()
        return self.listing(lid)

    def facets(self) -> dict:
        c = self.conn()
        try:
            base = "FROM listings WHERE status='active'"
            cats = {r[0]: r[1] for r in c.execute(f"SELECT category, COUNT(*) {base} GROUP BY 1")}
            models = [{"key": r[0], "n": r[1]} for r in c.execute(
                f"SELECT item_key, COUNT(*) n {base} AND item_key IS NOT NULL AND defect=0 GROUP BY 1 HAVING n>=3 ORDER BY n DESC, 1")]
            mode_n = {m: 0 for m in modes.MODE_TITLES}
            for (tags,) in c.execute(f"SELECT mode_tags {base} AND mode_tags IS NOT NULL"):
                for m in tags.split(","):
                    if m in mode_n:
                        mode_n[m] += 1
            total, clean, last, msk, fav = c.execute(
                f"SELECT COUNT(*), SUM(defect=0 AND wanted=0 AND junk IS NULL), MAX(last_seen), SUM(is_msk), SUM(fav) {base}").fetchone()
            now = time.time()
            gone_n, since = c.execute("SELECT SUM(status='inactive'), MIN(first_seen) FROM listings").fetchone()
            junk_n = c.execute(f"SELECT COUNT(*) {base} AND junk IS NOT NULL").fetchone()[0]
            stale_n = c.execute("SELECT COUNT(*) FROM listings WHERE status='active' AND last_seen<?",
                                (now - STALE_DAYS * 86400,)).fetchone()[0]
            ust = {r[0]: r[1] for r in c.execute(f"SELECT user_status, COUNT(*) {base} AND user_status IS NOT NULL GROUP BY 1")}
            return {
                "total": total, "clean": clean or 0, "last_seen": last, "fav": fav or 0,
                "gone": gone_n or 0, "stale": stale_n, "since": since, "junk": junk_n,
                "categories": [{"key": k, "title": t, "n": cats.get(k, 0)} for k, (t, _) in sources.CATEGORIES.items()
                               if cats.get(k)],
                "regions": [{"key": "msk", "title": "Москва", "n": msk or 0},
                            {"key": "regions", "title": "Другие регионы", "n": (total or 0) - (msk or 0)}],
                "models": models,
                "modes": [{"key": m, "title": t, "n": mode_n[m]} for m, t in modes.MODE_TITLES.items()],
                "user_statuses": ust, "filter_labels": FILTER_LABELS,
            }
        finally:
            c.close()

    def today(self) -> dict:
        """Дашборд «Сегодня»: новые кандидаты, лучший по прибыли, сколько лотов появилось и ушло, состояние сбора."""
        c = self.conn()
        try:
            now = time.time()
            lt = time.localtime(now)
            day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
            new_c = c.execute(
                "SELECT COUNT(DISTINCT c.listing_id) FROM candidates c JOIN listings l ON l.id=c.listing_id "
                "WHERE c.first_at>=? AND c.mode<>'manual' AND l.status='active'", (day0,)).fetchone()[0]
            where, args = build_query({"profit": ["1"]}, c)
            best = c.execute(f"SELECT {FIELDS} FROM listings WHERE {where} ORDER BY est_profit DESC LIMIT 5", args).fetchall()
            new_l = c.execute("SELECT COUNT(*) FROM listings WHERE first_seen>=?", (day0,)).fetchone()[0]
            gone = c.execute("SELECT COUNT(*) FROM listings WHERE status='inactive' AND gone_at>=?", (day0,)).fetchone()[0]
            gone_c = c.execute("SELECT COUNT(DISTINCT listing_id) FROM candidates WHERE inactive_at>=?", (day0,)).fetchone()[0]
            drops = c.execute(
                "SELECT COUNT(DISTINCT h.listing_id) FROM price_history h JOIN listings l ON l.id=h.listing_id "
                "WHERE h.ts>=? AND l.first_seen<h.ts AND l.price<l.first_price AND l.status='active'", (day0,)).fetchone()[0]
            cand_total = c.execute(f"SELECT COUNT(*) FROM listings WHERE {where}", args).fetchone()[0]
            return {"new_candidates": new_c, "candidates": cand_total, "best": [_row(r, now) for r in best],
                    "new_listings": new_l, "gone": gone, "gone_candidates": gone_c, "price_drops": drops,
                    "app": self.app_status()}
        finally:
            c.close()

    # ---------------- сохранённые поиски ----------------
    def saved(self) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute("SELECT id, name, query, notify FROM saved_searches ORDER BY id")]
        finally:
            c.close()

    def saved_change(self, data: dict) -> list[dict]:
        c = self.conn()
        try:
            if data.get("delete"):
                c.execute("DELETE FROM saved_searches WHERE id=?", (int(data["delete"]),))
            elif data.get("id") and "notify" in data:
                c.execute("UPDATE saved_searches SET notify=? WHERE id=?", (int(bool(data["notify"])), int(data["id"])))
            else:
                name = str(data.get("name") or "").strip()[:80]
                if not name:
                    raise ValueError("нужно название")
                c.execute("INSERT INTO saved_searches(name, query, created, notify) VALUES(?,?,?,?)",
                          (name, str(data.get("query") or "")[:2000], time.time(), int(bool(data.get("notify")))))
            c.commit()
        finally:
            c.close()
        return self.saved()

    # ---------------- сравнение ----------------
    def compare(self, q: dict) -> dict:
        ids = [i for i in (q.get("ids", [""])[0] or "").split(",") if i]
        c = self.conn()
        try:
            return compare.build(c, ids, ref=(q.get("ref", [""])[0] or None))
        finally:
            c.close()

    def compare_csv(self, q: dict) -> str:
        return compare.to_csv(self.compare(q), only_diff=(q.get("diff", [""])[0] == "1"))

    def compare_models(self, q: dict) -> dict:
        keys = [k for k in (q.get("keys", [""])[0] or "").split("|") if k]
        c = self.conn()
        try:
            return compare.models(c, keys)
        finally:
            c.close()

    def compare_sets(self) -> list[dict]:
        c = self.conn()
        try:
            return [{"id": r["id"], "name": r["name"], "ids": json.loads(r["ids"] or "[]"), "ref": r["ref"]}
                    for r in c.execute("SELECT * FROM compare_sets ORDER BY id DESC")]
        finally:
            c.close()

    def compare_sets_change(self, data: dict) -> list[dict]:
        c = self.conn()
        try:
            if data.get("delete"):
                c.execute("DELETE FROM compare_sets WHERE id=?", (int(data["delete"]),))
            else:
                ids = [str(i) for i in (data.get("ids") or [])][:compare.MAX_LOTS]
                name = str(data.get("name") or "").strip()[:80]
                if not name or len(ids) < 2:
                    raise ValueError("нужно название и хотя бы два лота")
                c.execute("INSERT INTO compare_sets(name, ids, ref, created) VALUES(?,?,?,?)",
                          (name, json.dumps(ids), str(data.get("ref") or "") or None, time.time()))
            c.commit()
        finally:
            c.close()
        return self.compare_sets()

    # ---------------- сделки ----------------
    def deals(self, q: dict) -> dict:
        days = _int(q, "days")
        c = self.conn()
        try:
            return db.deals_list(c, since=time.time() - days * 86400 if days else None)
        finally:
            c.close()

    def deals_change(self, data: dict) -> dict:
        c = self.conn()
        try:
            def num(k):
                v = data.get(k)
                return None if v in (None, "") else float(v)
            if data.get("delete"):
                db.deal_delete(c, int(data["delete"]))
            elif data.get("id"):
                fields = {k: num(k) for k in ("buy_price", "sell_price", "costs", "bought_at", "sold_at") if k in data}
                if "note" in data:
                    fields["note"] = str(data["note"] or "")[:1000]
                if "title" in data:
                    fields["title"] = str(data["title"] or "")[:200] or "Без названия"
                if fields.get("sell_price") and "sold_at" not in fields:
                    was = c.execute("SELECT sold_at FROM deals WHERE id=?", (int(data["id"]),)).fetchone()
                    if was and not was[0]:
                        fields["sold_at"] = time.time()
                if "sell_price" in fields and fields["sell_price"] is None:
                    fields["sold_at"] = None
                if not db.deal_update(c, int(data["id"]), **fields):
                    raise ValueError("сделка не найдена")
            else:
                db.deal_create(c, str(data["listing_id"]) if data.get("listing_id") else None, num("buy_price"),
                               str(data.get("note") or ""), str(data.get("title") or ""), num("costs") or 0.0, num("bought_at"))
            return db.deals_list(c, since=time.time() - _int({"d": [str(data.get("days") or "")]}, "d") * 86400
                                 if data.get("days") else None)
        finally:
            c.close()

    # ---------------- рынок ----------------
    def market(self, q: dict) -> dict:
        days = 30 if _int(q, "days", 7) >= 30 else 7
        cat = (q.get("cat", [""])[0] or "").strip()
        c = self.conn()
        try:
            now = time.time()
            cat_sql = " AND category=?" if cat in sources.CATEGORIES else ""
            cat_args = [cat] if cat_sql else []
            groups: dict[str, list[float]] = {}
            new_n: dict[str, int] = {}
            for key, price, first_seen in c.execute(
                    "SELECT item_key, price, first_seen FROM listings WHERE status='active' AND item_key IS NOT NULL "
                    f"AND price>0 AND defect=0 AND wanted=0 AND junk IS NULL{cat_sql}", cat_args):
                groups.setdefault(key, []).append(price)
                if first_seen >= now - days * 86400:
                    new_n[key] = new_n.get(key, 0) + 1
            since = time.strftime("%Y-%m-%d", time.localtime(now - days * 86400))
            hist: dict[str, list] = {}
            for key, day, med, n in c.execute("SELECT key, day, median, n FROM market_history WHERE day>=? ORDER BY day", (since,)):
                if key in groups:
                    hist.setdefault(key, []).append({"day": day, "median": med, "n": n})
            gone_rows: dict[str, list] = {}
            for r in c.execute("SELECT item_key, price, first_seen, posted_at, gone_at, baseline FROM listings "
                               "WHERE status='inactive' AND gone_at IS NOT NULL AND item_key IS NOT NULL AND defect=0 AND wanted=0"):
                gone_rows.setdefault(r["item_key"], []).append(r)
            table = analogs.load()
            cfg = Config.load(self.env_path)
            caps = analogs.ceilings(c, now, cfg.min_market_n, table) if cfg.analog_ceiling else {}
            rows = []
            for key, prices in groups.items():
                if len(prices) < 3:
                    continue
                h = hist.get(key, [])
                ms = db.market_stats(c, key, None, now)
                pts = price_points(ms, cfg, caps.get(key)) if ms.n >= cfg.min_market_n else None
                med = ms.median_ask if ms.n else statistics.median(prices)
                change = (med - h[0]["median"]) / h[0]["median"] if len(h) >= 2 and h[0]["median"] else None
                recent = [r for r in gone_rows.get(key, []) if r["gone_at"] >= now - 60 * 86400]
                lived = db.lifetimes(recent)
                sold = [r["price"] for r in gone_rows.get(key, []) if r["price"]]
                days_sell = max(0.5, statistics.median(lived)) if len(lived) >= db.LIQUIDITY_MIN else None
                level = "none" if days_sell is None else "high" if days_sell <= 5 else "medium" if days_sell <= 14 else "low"
                idx = analogs.index_of(key, table)
                rows.append({"key": key, "n": len(prices), "median": med, "min": min(db._trim(sorted(prices))),
                             "new": new_n.get(key, 0),
                             "gone": sum(1 for r in gone_rows.get(key, []) if r["gone_at"] >= now - days * 86400),
                             "change": change, "history": h,
                             "trust": "ok" if len(prices) >= 10 else "low" if len(prices) >= 6 else "none",
                             "liq_days": days_sell, "liq_level": level, "liq_n": len(lived),
                             "liq_rough": days_sell is not None and len(lived) < db.LIQUIDITY_SOLID,
                             "index": idx, "per_index": med / idx if idx else None,
                             "quick": pts.quick if pts else None, "comfort": pts.comfort if pts else None,
                             "basis": pts.basis if pts else "", "why": pts.why if pts else [],
                             "ceiling_key": pts.ceiling_key if pts else "", "used_only": ms.used_only, "new_n": ms.new_n,
                             "gone_total": len(gone_rows.get(key, [])),
                             "sold_median": statistics.median(sold) if len(sold) >= 3 else None})
            rows.sort(key=lambda r: (-r["n"], r["key"]))
            return {"days": days, "rows": rows[:300], "history_days": len({x["day"] for v in hist.values() for x in v}),
                    "analogs_approx": table["approx"], "analogs_error": table["error"]}
        finally:
            c.close()

    # ---------------- статус сбора ----------------
    def status(self) -> dict:
        c = self.conn()
        try:
            cycles = [dict(r) for r in c.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 60")]
            last_run: list[dict] = []
            for cy in cycles:                 # последний обход = подряд идущие источники с разрывом меньше 10 минут
                if last_run and last_run[-1]["started"] - cy["finished"] > 600:
                    break
                last_run.append(cy)
            agg = {k: sum(x[k] or 0 for x in last_run) for k in ("seen", "new", "rejected", "candidates", "captcha", "blocked")}
            week = time.time() - 7 * 86400
            wk = c.execute("SELECT COUNT(*), SUM(captcha), SUM(blocked), SUM(error IS NOT NULL), SUM(seen), SUM(new) "
                           "FROM cycles WHERE started>=?", (week,)).fetchone()
            errors = [dict(r) for r in c.execute(
                "SELECT started, source, error FROM cycles WHERE error IS NOT NULL ORDER BY id DESC LIMIT 10")]
            backups = sorted((p.name for p in (Path(self.db_path).resolve().parent / "backups").glob("*.db")), reverse=True)
            return {
                "last": {"started": last_run[-1]["started"] if last_run else None,
                         "finished": last_run[0]["finished"] if last_run else None, "sources": len(last_run), **agg},
                "week": {"sources": wk[0] or 0, "captcha": wk[1] or 0, "blocked": wk[2] or 0, "errors": wk[3] or 0,
                         "seen": wk[4] or 0, "new": wk[5] or 0},
                "cycles": cycles[:25], "errors": errors, "by_mode": modes.by_mode_report(c),
                "app": self.app_status(), "backups": backups[:10],
            }
        finally:
            c.close()

    # ---------------- источники, исключения, настройки ----------------
    def get_sources(self) -> dict:
        return {
            "sources": [{**s.__dict__, "url": s.url} for s in sources.load(self.sources_path)],
            "categories": [{"key": k, "title": t} for k, (t, _) in sources.CATEGORIES.items()],
            "regions": [{"key": k, "title": t} for k, t in sources.REGIONS.items()],
            "saved": (home() / self.sources_path).exists(),
        }

    def set_sources(self, items) -> dict:
        if not isinstance(items, list) or len(items) > 40:
            raise ValueError("ожидается список источников (не больше 40)")
        sources.save([sources.Source.from_dict(d) for d in items], self.sources_path)
        return self.get_sources()

    def excluded(self) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute("SELECT kind, value, title FROM excluded ORDER BY created DESC")]
        finally:
            c.close()

    def exclude(self, data: dict) -> list[dict]:
        c = self.conn()
        try:
            db.exclude(c, str(data.get("kind") or ""), str(data.get("value") or ""), str(data.get("title") or "")[:120],
                       on=data.get("on", True) is not False)
        finally:
            c.close()
        return self.excluded()

    def get_config(self) -> dict:
        cfg = Config.load(self.env_path)
        if self._version is None:
            self._version = version_info()
        return {
            "telegram": {"configured": bool(cfg.tg_token and cfg.tg_chat_id), "chat_id": cfg.tg_chat_id,
                         "token_set": bool(cfg.tg_token), "token_hint": ("…" + cfg.tg_token[-4:]) if cfg.tg_token else ""},
            "light_cycle": cfg.light_cycle, "light_min_sec": cfg.light_min_sec, "light_max_sec": cfg.light_max_sec,
            "full_interval_sec": cfg.full_interval_sec, "max_requests_per_hour": cfg.max_requests_per_hour,
            "version": self._version, "words_dir": str(home() / "words"), "analogs": analogs.load()["error"] or "",
        }

    def set_config(self, data: dict) -> dict:
        """Настройки в .env. Токен Telegram наружу не отдаётся: только признак, что он задан."""
        values: dict[str, str] = {}
        tg = data.get("telegram") or {}
        if "token" in tg and str(tg["token"]).strip():
            token = str(tg["token"]).strip()
            if ":" not in token or len(token) < 20 or any(ch.isspace() for ch in token):
                raise ValueError("токен бота выглядит неверно: он вида 123456789:AA… (выдаёт @BotFather)")
            values["TG_TOKEN"] = token
        if tg.get("clear"):
            values["TG_TOKEN"], values["TG_CHAT_ID"] = "", ""
        if "chat_id" in tg:
            chat = str(tg["chat_id"]).strip()
            if chat and not chat.lstrip("-").isdigit() and not chat.startswith("@"):
                raise ValueError("chat id — число (узнать у @userinfobot) или @имя_канала")
            values["TG_CHAT_ID"] = chat
        if "light_cycle" in data:
            values["LIGHT_CYCLE"] = "1" if data["light_cycle"] else "0"
        for env_key, (field, lo, hi) in CONFIG_KEYS.items():
            if field in data:
                try:
                    v = float(data[field])
                except (TypeError, ValueError):
                    raise ValueError(f"{field}: нужно число")
                if not lo <= v <= hi:
                    raise ValueError(f"{field}: допустимо от {lo} до {hi}")
                values[env_key] = str(int(v))
        if "LIGHT_MIN_SEC" in values or "LIGHT_MAX_SEC" in values:
            cfg = Config.load(self.env_path)
            lo = float(values.get("LIGHT_MIN_SEC", cfg.light_min_sec))
            hi = float(values.get("LIGHT_MAX_SEC", cfg.light_max_sec))
            if hi < lo:
                raise ValueError("интервал лёгкого цикла: «до» не может быть меньше «от»")
        if values:
            set_env(self.env_path, values)
        return self.get_config()

    def telegram_test(self) -> dict:
        cfg = Config.load(self.env_path)
        ok, msg = Notifier(cfg.tg_token, cfg.tg_chat_id).test()
        return {"ok": ok, "message": msg}

    def prefs(self) -> dict:
        c = self.conn()
        try:
            return {k: db.get_setting(c, k) for k in PREF_KEYS}
        finally:
            c.close()

    def set_pref(self, data: dict) -> dict:
        key = str(data.get("key") or "")
        if key not in PREF_KEYS or len(json.dumps(data.get("value"))) > 20000:
            raise ValueError("неизвестная настройка вида")
        c = self.conn()
        try:
            db.set_setting(c, key, data.get("value"))
        finally:
            c.close()
        return self.prefs()

    # ---------------- экспорт ----------------
    def export_csv(self, q: dict) -> str:
        c = self.conn()
        try:
            where, args = build_query(q, c)
            rows = c.execute(f"SELECT {FIELDS} FROM listings WHERE {where} ORDER BY {order_by(q)}, id LIMIT 20000", args).fetchall()
        finally:
            c.close()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(["id", "Название", "Цена", "Категория", "Позиция", "Медиана", "n", "Скидка к рынку, %", "Быстрая продажа",
                    "Спокойная продажа", "Мусор", "Расчётная прибыль",
                    "Режимы", "Доверие", "Причина", "Город", "Доставка", "Состояние", "Дата", "Дней в базе", "Снижение цены",
                    "Продавец", "Рейтинг", "Мой статус", "Заметка", "Объявление", "Впервые увидели", "Последний раз видели",
                    "Опубликовано (оценка)", "Ушло", "Прожило дней", "Ссылка"])
        day = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
        now = time.time()
        for r in rows:
            d = _row(r, now)
            sc = d["score"] or {}
            w.writerow([d["id"], d["title"], f"{d['price']:.0f}" if d["price"] else "", d["category_title"], d["item_key"] or "",
                        f"{sc['median_ask']:.0f}" if sc.get("market_n") else "", sc.get("market_n") or "",
                        f"{sc['discount_vs_market'] * 100:.0f}" if sc.get("discount_vs_market") is not None and sc.get("market_n") else "",
                        f"{sc['quick_price']:.0f}" if sc.get("quick_price") else "",
                        f"{sc['comfort_price']:.0f}" if sc.get("comfort_price") else "", d["junk_title"],
                        f"{d['est_profit']:.0f}" if d["est_profit"] else "", ", ".join(m["title"] for m in d["modes"]),
                        modes.CONFIDENCE_RU.get(d["confidence"] or "", ""), d["reason"] or "", d["city"] or "",
                        "да" if d["delivery"] else "нет", d["avito_condition"] or "", d["date_text"] or "",
                        f"{d['days_listed']:.0f}" if d["days_listed"] is not None else "",
                        f"{d['price_drop']:.0f}" if d["price_drop"] else "", d["seller_name"] or "",
                        d["rating"] if d["rating"] is not None else "", d["user_status"] or "", d["note"] or "",
                        "ушло" if d["gone"] else "давно не видели" if d["stale"] else "активно",
                        day(d["first_seen"]), day(d["last_seen"]), day(d["posted_at"]), day(d["gone_at"]) if d["gone"] else "",
                        f"{d['lifetime_days']:.1f}" if d["lifetime_days"] is not None else "", d["url"] or ""])
        return "﻿" + buf.getvalue()

    # ---------------- обход / приложение ----------------
    def app_status(self) -> dict:
        if self.desktop:
            return self.desktop.status()
        st = self.scan_status()
        return {"desktop": False, "state": "running" if st["running"] else "idle", "paused": False,
                "status_line": "идёт обход…" if st["running"] else "обход запускается вручную или командой watch",
                "autostart": False, "home": str(home())}

    def app_action(self, data: dict) -> dict:
        action = str((data or {}).get("action") or "")
        if self.desktop:
            return self.desktop.action(action)
        if action == "run_now":
            self.start_scan()
            return self.app_status()
        if action == "check_updates":
            from .app import check_updates
            return {**self.app_status(), "update": check_updates()}
        raise ValueError("доступно только в приложении: python -m gpuflip app")

    def scan_status(self) -> dict:
        running = self.scan_proc is not None and self.scan_proc.poll() is None
        tail = ""
        for name in (SCAN_LOG, "watch.log"):
            p = home() / name
            if p.exists():
                tail = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
                if tail:
                    break
        return {"running": running, "started": self.scan_started, "log": tail,
                "wait": max(0, int(self.scan_started + MIN_SCAN_GAP - time.time())) if not running else 0}

    def start_scan(self) -> dict:
        st = self.scan_status()
        if st["running"]:
            raise ValueError("обход уже идёт")
        if st["wait"]:
            raise ValueError(f"слишком часто: подождите ещё {st['wait'] // 60 + 1} мин, иначе Авито покажет капчу")
        log = open(home() / SCAN_LOG, "a", encoding="utf-8")
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} обход из интерфейса ===\n")
        log.flush()
        self.scan_proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "gpuflip", "scan", "--dry-run"],
                                          stdout=log, stderr=subprocess.STDOUT)
        self.scan_started = time.time()
        return self.scan_status()


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):   # тишина в консоли
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _csv(self, text: str, name: str) -> None:
            self._send(200, text.encode("utf-8"), "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{name}"'})

        def _local(self) -> bool:
            """Только свои запросы: чужой сайт не должен дёргать API через браузер пользователя."""
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            ok_origin = origin is None or urlparse(origin).hostname in ("127.0.0.1", "localhost")
            return host in ("127.0.0.1", "localhost") and ok_origin

        def _static(self, name: str) -> None:
            p = (WEB_DIR / name).resolve()
            if p.parent != WEB_DIR.resolve() or p.suffix not in STATIC_TYPES or not p.is_file():
                return self._json({"error": "not found"}, 404)
            self._send(200, p.read_bytes(), STATIC_TYPES[p.suffix])

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._local():
                return self._json({"error": "forbidden"}, 403)
            routes = {
                "/api/listings": lambda: app.listings(q),
                "/api/listing": lambda: app.listing((q.get("id", [""])[0])),
                "/api/facets": app.facets,
                "/api/today": app.today,
                "/api/saved": app.saved,
                "/api/market": lambda: app.market(q),
                "/api/status": app.status,
                "/api/sources": app.get_sources,
                "/api/scan": app.scan_status,
                "/api/app": app.app_status,
                "/api/compare": lambda: app.compare(q),
                "/api/models": lambda: app.compare_models(q),
                "/api/compare_sets": app.compare_sets,
                "/api/deals": lambda: app.deals(q),
                "/api/config": app.get_config,
                "/api/excluded": app.excluded,
                "/api/prefs": app.prefs,
                "/api/analogs": analogs.load,
            }
            try:
                if u.path == "/":
                    return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if u.path.startswith("/static/"):
                    return self._static(u.path[len("/static/"):])
                if u.path == "/api/export.csv":
                    return self._csv(app.export_csv(q), f"gpuflip-{time.strftime('%Y%m%d-%H%M')}.csv")
                if u.path == "/api/compare.csv":
                    return self._csv(app.compare_csv(q), f"gpuflip-compare-{time.strftime('%Y%m%d-%H%M')}.csv")
                if u.path in routes:
                    return self._json(routes[u.path]())
                self._json({"error": "not found"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:   # интерфейсу нужен ответ, а не обрыв соединения
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._local() or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json({"error": "forbidden"}, 403)
            routes = {
                "/api/sources": app.set_sources,
                "/api/scan": lambda _d: app.start_scan(),
                "/api/listing": app.update_listing,
                "/api/saved": app.saved_change,
                "/api/app": app.app_action,
                "/api/compare_sets": app.compare_sets_change,
                "/api/deals": app.deals_change,
                "/api/config": app.set_config,
                "/api/telegram_test": lambda _d: app.telegram_test(),
                "/api/exclude": app.exclude,
                "/api/prefs": app.set_pref,
            }
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(n, 1_000_000)) or b"null")
                if u.path in routes:
                    return self._json(routes[u.path](data))
                self._json({"error": "not found"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return H


def serve(db_path: str, port: int = 8765, open_browser: bool = True) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(App(db_path)))
    url = f"http://127.0.0.1:{port}/"
    print(f"Интерфейс: {url}  (Ctrl+C — остановить)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
