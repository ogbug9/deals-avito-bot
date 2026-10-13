"""Локальный интерфейс: объявления с фильтрами, рынок, статус сбора, настройки.

Только стандартная библиотека. Слушает 127.0.0.1 — снаружи не виден.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, modes, sources
from .paths import home, package_dir
from .query import FILTER_LABELS, SORTS, USER_STATUSES, _int, build_query, order_by

HTML_PATH = package_dir() / "ui.html"
SCAN_LOG = "scan.log"
MIN_SCAN_GAP = 600          # не чаще раза в 10 минут: частые обходы = капча Авито
FIELDS = ("id, url, title, description, city, price, first_price, category, region, delivery, image, date_text, "
          "posted_at, first_seen, last_seen, rating, reviews, defect, wanted, gpu_key, item_key, fine_key, unit_price, qty, "
          "flags, net_profit, score_json, status, mode_tags, est_profit, est_profit_pct, reason, confidence, model_source, "
          "urgent, is_msk, avito_condition, fav, user_status, note, seller_id, seller_name, seller_type, seller_ads, "
          "reseller, photos, deep_at, avito_model, checklist, brand, gone_at, checked_at")


def _row(r) -> dict:
    d = {k: r[k] for k in r.keys() if k not in ("score_json", "flags")}
    d["flags"] = json.loads(r["flags"] or "{}")
    sc = json.loads(r["score_json"]) if r["score_json"] else None
    d["score"] = sc and {k: sc.get(k) for k in ("ok", "reason", "median_ask", "market_n", "net", "net_pct", "target_buy",
                                                 "sell_price", "risk_loss", "discount_vs_market", "low_data")}
    d["category_title"] = sources.CATEGORIES.get(r["category"], (r["category"] or "—",))[0]
    d["modes"] = [{"key": m, "title": modes.MODE_TITLES.get(m, m)} for m in (r["mode_tags"] or "").split(",") if m]
    return d


class App:
    def __init__(self, db_path: str, sources_path: str = "sources.json", desktop=None):
        self.db_path, self.sources_path, self.desktop = db_path, sources_path, desktop
        self.scan_proc: subprocess.Popen | None = None
        self.scan_started = 0.0

    def conn(self):
        return db.connect(self.db_path)

    # ---------------- объявления ----------------
    def listings(self, q: dict) -> dict:
        where, args = build_query(q)
        per = min(100, max(1, _int(q, "per", 40)))
        page = max(1, _int(q, "page", 1))
        order = SORTS.get((q.get("sort", ["date"])[0]), SORTS["date"])
        c = self.conn()
        try:
            total, pmin, pmax = c.execute(f"SELECT COUNT(*), MIN(price), MAX(price) FROM listings WHERE {where}", args).fetchone()
            rows = c.execute(f"SELECT {FIELDS} FROM listings WHERE {where} ORDER BY {order}, id LIMIT ? OFFSET ?",
                             args + [per, (page - 1) * per]).fetchall()
            out = {"total": total, "page": page, "per": per, "price_min": pmin, "price_max": pmax,
                   "items": [_row(r) for r in rows]}
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
            where, args = build_query({k: v for k, v in q.items() if k != key})
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
            d = _row(r)
            d["price_history"] = [{"ts": ts, "price": p} for ts, p in c.execute(
                "SELECT ts, price FROM price_history WHERE listing_id=? ORDER BY ts", (lid,))]
            d["market_history"] = [{"day": day, "median": m, "n": n} for day, m, n in c.execute(
                "SELECT day, median, n FROM market_history WHERE key=? ORDER BY day", (r["item_key"] or "",))]
            d["candidates"] = [dict(x) for x in c.execute(
                "SELECT mode, est_profit, est_profit_pct, reason, confidence, first_at, checked_at, inactive_at "
                "FROM candidates WHERE listing_id=? ORDER BY est_profit DESC", (lid,))]
            for x in d["candidates"]:
                x["title"] = modes.MODE_TITLES.get(x["mode"], x["mode"])
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
                f"SELECT COUNT(*), SUM(defect=0 AND wanted=0), MAX(last_seen), SUM(is_msk), SUM(fav) {base}").fetchone()
            ust = {r[0]: r[1] for r in c.execute(f"SELECT user_status, COUNT(*) {base} AND user_status IS NOT NULL GROUP BY 1")}
            return {
                "total": total, "clean": clean or 0, "last_seen": last, "fav": fav or 0,
                "categories": [{"key": k, "title": t, "n": cats.get(k, 0)} for k, (t, _) in sources.CATEGORIES.items()
                               if cats.get(k)],
                "regions": [{"key": "msk", "title": "Москва", "n": msk or 0},
                            {"key": "regions", "title": "Другие регионы", "n": (total or 0) - (msk or 0)}],
                "models": models,
                "modes": [{"key": m, "title": t, "n": mode_n[m]} for m, t in modes.MODE_TITLES.items()],
                "user_statuses": ust,
            }
        finally:
            c.close()

    # ---------------- сохранённые поиски ----------------
    def saved(self) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute("SELECT id, name, query FROM saved_searches ORDER BY id")]
        finally:
            c.close()

    def saved_change(self, data: dict) -> list[dict]:
        c = self.conn()
        try:
            if data.get("delete"):
                c.execute("DELETE FROM saved_searches WHERE id=?", (int(data["delete"]),))
            else:
                name = str(data.get("name") or "").strip()[:80]
                if not name:
                    raise ValueError("нужно название")
                c.execute("INSERT INTO saved_searches(name, query, created) VALUES(?,?,?)",
                          (name, str(data.get("query") or "")[:2000], time.time()))
            c.commit()
        finally:
            c.close()
        return self.saved()

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
                    f"AND price>0 AND defect=0 AND wanted=0{cat_sql}", cat_args):
                groups.setdefault(key, []).append(price)
                if first_seen >= now - days * 86400:
                    new_n[key] = new_n.get(key, 0) + 1
            since = time.strftime("%Y-%m-%d", time.localtime(now - days * 86400))
            hist: dict[str, list] = {}
            for key, day, med, n in c.execute("SELECT key, day, median, n FROM market_history WHERE day>=? ORDER BY day", (since,)):
                if key in groups:
                    hist.setdefault(key, []).append({"day": day, "median": med, "n": n})
            gone = {r[0]: r[1] for r in c.execute(
                "SELECT item_key, COUNT(*) FROM listings WHERE status='inactive' AND gone_at>=? AND item_key IS NOT NULL GROUP BY 1",
                (now - days * 86400,))}
            rows = []
            for key, prices in groups.items():
                if len(prices) < 3:
                    continue
                h = hist.get(key, [])
                med = statistics.median(prices)
                change = (med - h[0]["median"]) / h[0]["median"] if len(h) >= 2 and h[0]["median"] else None
                rows.append({"key": key, "n": len(prices), "median": med, "min": min(prices), "new": new_n.get(key, 0),
                             "gone": gone.get(key, 0), "change": change, "history": h,
                             "trust": "ok" if len(prices) >= 10 else "low" if len(prices) >= 6 else "none"})
            rows.sort(key=lambda r: (-r["n"], r["key"]))
            return {"days": days, "rows": rows[:200], "history_days": len({x["day"] for v in hist.values() for x in v})}
        finally:
            c.close()

    # ---------------- статус сбора ----------------
    def status(self) -> dict:
        c = self.conn()
        try:
            cycles = [dict(r) for r in c.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 40")]
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
            return {
                "last": {"started": last_run[-1]["started"] if last_run else None,
                         "finished": last_run[0]["finished"] if last_run else None, "sources": len(last_run), **agg},
                "week": {"sources": wk[0] or 0, "captcha": wk[1] or 0, "blocked": wk[2] or 0, "errors": wk[3] or 0,
                         "seen": wk[4] or 0, "new": wk[5] or 0},
                "cycles": cycles[:20], "errors": errors, "by_mode": modes.by_mode_report(c),
                "app": self.app_status(),
            }
        finally:
            c.close()

    # ---------------- источники ----------------
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

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _local(self) -> bool:
            """Только свои запросы: чужой сайт не должен дёргать API через браузер пользователя."""
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            ok_origin = origin is None or urlparse(origin).hostname in ("127.0.0.1", "localhost")
            return host in ("127.0.0.1", "localhost") and ok_origin

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._local():
                return self._json({"error": "forbidden"}, 403)
            routes = {
                "/api/listings": lambda: app.listings(q),
                "/api/listing": lambda: app.listing((q.get("id", [""])[0])),
                "/api/facets": app.facets,
                "/api/saved": app.saved,
                "/api/market": lambda: app.market(q),
                "/api/status": app.status,
                "/api/sources": app.get_sources,
                "/api/scan": app.scan_status,
                "/api/app": app.app_status,
            }
            try:
                if u.path == "/":
                    return self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
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
