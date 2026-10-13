"""Локальный интерфейс: список объявлений с фильтрами и настройка источников сбора.

Только стандартная библиотека. Слушает 127.0.0.1 — снаружи не виден.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, sources

HTML_PATH = Path(__file__).with_name("ui.html")
SCAN_LOG = "scan.log"
MIN_SCAN_GAP = 600          # не чаще раза в 10 минут: частые обходы = капча Авито
SORTS = {
    "date": "COALESCE(posted_at, first_seen) DESC",
    "price_asc": "price ASC",
    "price_desc": "price DESC",
    "profit": "net_profit IS NULL, net_profit DESC",
    "rating": "rating IS NULL, rating DESC, reviews DESC",
}
FIELDS = ("id, url, title, description, city, price, first_price, category, region, delivery, image, date_text, "
          "posted_at, first_seen, last_seen, rating, reviews, defect, wanted, gpu_key, flags, net_profit, score_json, status")


def _int(q: dict, name: str, default: int = 0) -> int:
    try:
        return int(float(q.get(name, [""])[0] or default))
    except ValueError:
        return default


def build_query(q: dict) -> tuple[str, list]:
    """Параметры запроса → (WHERE, аргументы). q — как из parse_qs."""
    one = lambda k: (q.get(k, [""])[0] or "").strip()
    where, args = ["1=1"], []
    if one("status") != "all":
        where.append("status='active'")
    cats = [c for c in one("cat").split(",") if c in sources.CATEGORIES]
    if cats:
        where.append(f"category IN ({','.join('?' * len(cats))})")
        args += cats
    if one("region"):
        where.append("region=?")
        args.append(one("region"))
    if _int(q, "pmin"):
        where.append("price>=?")
        args.append(_int(q, "pmin"))
    if _int(q, "pmax"):
        where.append("price<=?")
        args.append(_int(q, "pmax"))
    if one("delivery") == "1":
        where.append("delivery=1")
    if one("defect") != "show":            # по умолчанию брак скрыт
        where.append("defect=0")
    if one("wanted") != "show":
        where.append("wanted=0")
    if one("model"):
        where.append("gpu_key=?")
        args.append(one("model"))
    if one("profit") == "1":
        where.append("net_profit>0")
    if one("rating"):
        try:
            args.append(float(one("rating")))
            where.append("rating>=?")
        except ValueError:
            pass
    if _int(q, "age"):
        where.append("COALESCE(posted_at, first_seen)>=?")
        args.append(time.time() - _int(q, "age") * 86400)
    if one("city"):
        where.append("lc(city) LIKE ?")
        args.append(f"%{one('city').lower()}%")
    for word in one("q").lower().split()[:8]:
        where.append("lc(title || ' ' || COALESCE(description, '')) LIKE ?")
        args.append(f"%{word}%")
    return " AND ".join(where), args


def _row(r) -> dict:
    d = {k: r[k] for k in r.keys() if k not in ("score_json", "flags")}
    d["flags"] = list(json.loads(r["flags"] or "{}"))
    sc = json.loads(r["score_json"]) if r["score_json"] else None
    d["score"] = sc and {k: sc.get(k) for k in ("ok", "reason", "median_ask", "market_n", "net", "net_pct", "target_buy")}
    d["category_title"] = sources.CATEGORIES.get(r["category"], (r["category"] or "—",))[0]
    return d


class App:
    def __init__(self, db_path: str, sources_path: str = "sources.json"):
        self.db_path, self.sources_path = db_path, sources_path
        self.scan_proc: subprocess.Popen | None = None
        self.scan_started = 0.0

    def conn(self):
        c = db.connect(self.db_path)
        c.create_function("lc", 1, lambda s: (s or "").lower())   # LIKE в SQLite не умеет регистр кириллицы
        return c

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
            return {"total": total, "page": page, "per": per, "price_min": pmin, "price_max": pmax,
                    "items": [_row(r) for r in rows]}
        finally:
            c.close()

    def facets(self) -> dict:
        c = self.conn()
        try:
            base = "FROM listings WHERE status='active'"
            cats = {r[0]: r[1] for r in c.execute(f"SELECT category, COUNT(*) {base} GROUP BY 1")}
            regs = {r[0]: r[1] for r in c.execute(f"SELECT region, COUNT(*) {base} GROUP BY 1")}
            models = [{"key": r[0], "n": r[1]} for r in c.execute(
                f"SELECT gpu_key, COUNT(*) n {base} AND gpu_key IS NOT NULL AND defect=0 GROUP BY 1 ORDER BY n DESC, 1")]
            total, clean, last = c.execute(f"SELECT COUNT(*), SUM(defect=0 AND wanted=0), MAX(last_seen) {base}").fetchone()
            return {
                "total": total, "clean": clean or 0, "last_seen": last,
                "categories": [{"key": k, "title": t, "n": cats.get(k, 0)} for k, (t, _) in sources.CATEGORIES.items()
                               if cats.get(k)],
                "regions": [{"key": k, "title": sources.REGIONS.get(k, k), "n": n} for k, n in regs.items() if k],
                "models": models,
            }
        finally:
            c.close()

    def get_sources(self) -> dict:
        return {
            "sources": [{**s.__dict__, "url": s.url} for s in sources.load(self.sources_path)],
            "categories": [{"key": k, "title": t} for k, (t, _) in sources.CATEGORIES.items()],
            "regions": [{"key": k, "title": t} for k, t in sources.REGIONS.items()],
            "saved": Path(self.sources_path).exists(),
        }

    def set_sources(self, items) -> dict:
        if not isinstance(items, list) or len(items) > 40:
            raise ValueError("ожидается список источников (не больше 40)")
        sources.save([sources.Source.from_dict(d) for d in items], self.sources_path)
        return self.get_sources()

    def scan_status(self) -> dict:
        running = self.scan_proc is not None and self.scan_proc.poll() is None
        tail = ""
        if Path(SCAN_LOG).exists():
            tail = "\n".join(Path(SCAN_LOG).read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
        return {"running": running, "started": self.scan_started, "log": tail,
                "wait": max(0, int(self.scan_started + MIN_SCAN_GAP - time.time())) if not running else 0}

    def start_scan(self) -> dict:
        st = self.scan_status()
        if st["running"]:
            raise ValueError("обход уже идёт")
        if st["wait"]:
            raise ValueError(f"слишком часто: подождите ещё {st['wait'] // 60 + 1} мин, иначе Авито покажет капчу")
        log = open(SCAN_LOG, "a", encoding="utf-8")
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
            try:
                if u.path == "/":
                    return self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/listings":
                    return self._json(app.listings(q))
                if u.path == "/api/facets":
                    return self._json(app.facets())
                if u.path == "/api/sources":
                    return self._json(app.get_sources())
                if u.path == "/api/scan":
                    return self._json(app.scan_status())
                self._json({"error": "not found"}, 404)
            except Exception as e:   # интерфейсу нужен ответ, а не обрыв соединения
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._local() or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json({"error": "forbidden"}, 403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(n, 1_000_000)) or b"null")
                if u.path == "/api/sources":
                    return self._json(app.set_sources(data))
                if u.path == "/api/scan":
                    return self._json(app.start_scan())
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
