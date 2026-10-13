"""CLI: scan | watch | report | probe | deal."""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time

from . import db, sources
from .collector import AvitoCollector, BlockedError, FetchError, parse_search_html, read_csv
from .config import Config
from .notifier import Notifier, fmt_rub
from . import modes, wordlists
from .pipeline import process, recheck


def _collector(cfg: Config) -> AvitoCollector:
    return AvitoCollector(cfg.proxy, cfg.delay_sec, cfg.max_pages, cfg.chrome_profile, cfg.headless, cfg.captcha_wait)


def _notifier(cfg: Config, dry: bool) -> Notifier:
    return Notifier("" if dry else cfg.tg_token, "" if dry else cfg.tg_chat_id)


def cmd_scan(args, cfg: Config) -> int:
    conn = db.connect(cfg.db_path)
    notifier = _notifier(cfg, args.dry_run)
    if args.csv:
        stats = process(conn, cfg, read_csv(args.csv), source=f"csv:{args.csv}", complete=False, notifier=notifier)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if args.url:
        todo = [(args.url, args.url, cfg.max_pages, "", "")]
    else:
        todo = source_plan(cfg)
    return _run_sources(conn, cfg, notifier, todo)


def source_plan(cfg: Config) -> list[tuple]:
    """Источники обхода: sources.json + регионы из REGIONS (режим 2, только видеокарты с Авито Доставкой)."""
    srcs = [s for s in sources.load() if s.enabled]
    if cfg.mode_regional:
        have = {(s.category, s.region) for s in srcs}
        for r in cfg.regions:
            if ("videokarty", r) not in have:
                srcs.append(sources.Source.from_dict({"name": f"Видеокарты — {sources.REGIONS.get(r, r)}, с доставкой",
                                                      "category": "videokarty", "region": r, "delivery_only": True,
                                                      "max_pages": cfg.region_max_pages}))
    return [(s.name, s.url, s.max_pages, s.category, s.region) for s in srcs]


def _run_sources(conn, cfg: Config, notifier, todo, target: int = 0, record_alerts: bool = True,
                 on_captcha=None, should_stop=lambda: False) -> int:
    """todo: (название, ссылка, страниц, категория, регион). target>0 — остановиться, когда в базе столько активных."""
    rc = 0
    with _collector(cfg) as col:
        col.on_captcha = on_captcha
        for n, (name, url, pages, category, region) in enumerate(todo):
            if should_stop() or (target and _active(conn) >= target):
                break
            if n:
                time.sleep(random.uniform(cfg.delay_sec * 2, cfg.delay_sec * 4))   # пауза между разделами
            started, cap0 = time.time(), col.captchas
            try:
                items, complete = col.scan(url, pages)
            except BlockedError as e:
                _log_cycle(conn, started, name, captcha=col.captchas - cap0, blocked=1, error=str(e))
                print(f"⛔ {e}. Снизьте частоту, смените IP/прокси или используйте --csv.", file=sys.stderr, flush=True)
                return 3   # дальше по списку идти бессмысленно — только продлим блокировку
            except FetchError as e:
                _log_cycle(conn, started, name, captcha=col.captchas - cap0, error=str(e))
                print(f"⛔ Браузер/сеть: {e}", file=sys.stderr, flush=True)
                rc = 5
                continue
            stats = process(conn, cfg, items, source=url, complete=complete, notifier=notifier,
                            category=category, region=region, record_alerts=record_alerts)
            _log_cycle(conn, started, name, stats, captcha=col.captchas - cap0)
            print(f"{name}: {json.dumps(stats, ensure_ascii=False)} | в базе {_active(conn)}", flush=True)
        if rc == 0 and not target and not should_stop() and cfg.recheck_per_cycle > 0:
            def check(u):
                time.sleep(random.uniform(cfg.delay_sec, cfg.delay_sec * 2))
                return col.check_listing(u)
            res = recheck(conn, cfg, check)
            if res["checked"]:
                print(f"Перепроверка карточек: {json.dumps(res, ensure_ascii=False)}", flush=True)
    return rc


def _log_cycle(conn, started: float, name: str, stats: dict | None = None, captcha: int = 0, blocked: int = 0,
               error: str = "") -> None:
    s = stats or {}
    conn.execute("INSERT INTO cycles(started,finished,source,seen,new,rejected,candidates,captcha,blocked,error) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (started, time.time(), name, s.get("seen", 0), s.get("new", 0), s.get("seen", 0) - s.get("parsed_ok", 0),
                  s.get("candidates", 0), captcha, blocked, error or None))
    conn.commit()


def _active(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM listings WHERE status='active'").fetchone()[0]


def cmd_collect(args, cfg: Config) -> int:
    """Первичный набор базы: глубже обычного обхода, пока не наберётся --target объявлений."""
    conn = db.connect(cfg.db_path)
    srcs = [s for s in sources.load() if s.enabled]
    need = args.target - _active(conn)
    if need <= 0:
        print(f"В базе уже {_active(conn)} активных объявлений (цель {args.target}).")
        return 0
    # ~45 новых объявлений со страницы; страницы делим между источниками пропорционально их max_pages
    factor = max(1.0, need / 45 * 1.15 / sum(s.max_pages for s in srcs))
    todo = [(s.name, s.url, min(20, math.ceil(s.max_pages * factor)), s.category, s.region) for s in srcs]
    print(f"Нужно ещё ~{need}; план: " + ", ".join(f"{t[0]} ×{t[2]}" for t in todo), flush=True)
    return _run_sources(conn, cfg, _notifier(cfg, True) if args.dry_run else None, todo, target=args.target,
                        record_alerts=False)   # при наборе базы алерты не «сжигаем»: их покажет обычный обход


def cmd_ui(args, cfg: Config) -> int:
    from .ui import serve
    serve(cfg.db_path, args.port, open_browser=not args.no_browser)
    return 0


def cmd_watch(args, cfg: Config) -> int:
    while True:
        rc = cmd_scan(args, cfg)
        pause = args.interval * random.uniform(0.8, 1.3) * (4 if rc == 3 else 1)
        print(f"сплю {pause / 60:.0f} мин")
        time.sleep(pause)


def report_by_mode(conn) -> None:
    print("По режимам (просмотрено / кандидатов / средняя оценка прибыли / ушло к перепроверке):")
    for r in modes.by_mode_report(conn):
        share = "—" if r["inactive_share"] is None else f"{r['inactive_share']:.0%} ({r['inactive']} из {r['checked']})"
        print(f"  {r['title']:<20} просмотрено {r['viewed']:<6} кандидатов {r['candidates']:<4} "
              f"средняя прибыль {fmt_rub(r['avg_profit']):>10}  inactive: {share}")
        if r["rejected"]:
            print("      отсеяно: " + ", ".join(f"{k} {v}" for k, v in r["rejected"].items()))


def cmd_report(args, cfg: Config) -> int:
    conn = db.connect(cfg.db_path)
    if args.by_mode:
        report_by_mode(conn)
        return 0
    rows = conn.execute(
        "SELECT title, item_key, price, url, mode_tags, est_profit, est_profit_pct, reason, confidence FROM listings "
        "WHERE status='active' AND mode_tags IS NOT NULL AND mode_tags<>'manual' ORDER BY est_profit DESC LIMIT ?",
        (args.top,)).fetchall()
    tot = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    act = conn.execute("SELECT COUNT(*) c FROM listings WHERE status='active'").fetchone()["c"]
    print(f"В базе {tot} объявлений, активных {act}. Кандидатов: {len(rows)}")
    for r in rows:
        tags = ", ".join(modes.MODE_TITLES.get(m, m) for m in r["mode_tags"].split(","))
        print(f"- [{tags}] {r['item_key'] or r['title'][:40]:<22} {fmt_rub(r['price']):>10}  чистыми ≈ "
              f"+{fmt_rub(r['est_profit'])} ({r['est_profit_pct']:.0%}), доверие: {modes.CONFIDENCE_RU.get(r['confidence'], '—')}"
              f"\n  {r['reason']}\n  {r['url']}")
    manual = conn.execute("SELECT title, price, url FROM listings WHERE status='active' AND mode_tags='manual' "
                          "ORDER BY price LIMIT ?", (args.top,)).fetchall()
    if manual:
        print(f"\nПроверить вручную (модель не определена, цена низкая): {len(manual)}")
        for r in manual:
            print(f"  {fmt_rub(r['price']):>10}  {r['title'][:60]}\n  {r['url']}")
    print("\nПо моделям (n активных / медиана):")
    for r in conn.execute(
        "SELECT gpu_key, COUNT(*) n, AVG(price) a FROM listings WHERE status='active' AND gpu_key IS NOT NULL "
        "GROUP BY gpu_key ORDER BY n DESC LIMIT 15"
    ):
        ms = db.market_stats(conn, r["gpu_key"], None, time.time())
        print(f"  {r['gpu_key']:<18} n={ms.n:<3} медиана {fmt_rub(ms.median_ask)}  ушло: {ms.sold_n}")
    s = db.deal_stats(conn)
    print(f"\nСделки: закрыто {s['closed']}, открыто {s['open']}, прибыль {fmt_rub(s['profit'])}, "
          f"ROI {s['roi']:.0%}, средний оборот {s['avg_days']:.0f} дн. (цель перед фазой 2: 20 закрытых)")
    return 0


PROBE_DUMP = "probe.html"


def _page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return " ".join(m.group(1).split())[:120] if m else "—"


def cmd_probe(args, cfg: Config) -> int:
    code = 0
    with _collector(cfg) as col:
        try:
            items = parse_search_html(col.fetch(args.url))
        except BlockedError as e:
            print("⛔", e)
            items, code = [], 3
        except FetchError as e:
            print("⛔ Браузер/сеть:", e)
            return 5
    print(f"HTTP {col.last_status} | {len(col.last_html)} байт | title: {_page_title(col.last_html)}")
    if not items:
        with open(PROBE_DUMP, "w", encoding="utf-8") as f:
            f.write(col.last_html)
        print(f"HTML сохранён в {PROBE_DUMP}")
    if code:
        return code
    print(f"Найдено объявлений: {len(items)}")
    for i in items[:5]:
        print(f"  {i.id} | {i.price:.0f} | {i.title}")
    if not items:
        print("0 объявлений: вёрстка изменилась или это заглушка.")
        return 4
    return 0


def cmd_deal(args, cfg: Config) -> int:
    conn = db.connect(cfg.db_path)
    if args.action == "buy":
        did = db.deal_buy(conn, args.ref, float(args.price), args.note or "")
        print(f"Сделка #{did} открыта")
    elif args.action == "sell":
        ok = db.deal_sell(conn, int(args.ref), float(args.price), float(args.costs or 0))
        print("Закрыта" if ok else "Сделка не найдена или уже закрыта")
    else:
        print(json.dumps(db.deal_stats(conn), ensure_ascii=False, indent=2))
    conn.commit()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gpuflip")
    ap.add_argument("--env", default=".env")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("scan", cmd_scan), ("watch", cmd_watch)):
        p = sub.add_parser(name)
        p.add_argument("--url")
        p.add_argument("--csv")
        p.add_argument("--dry-run", action="store_true", help="не слать в Telegram, печатать в консоль")
        if name == "watch":
            p.add_argument("--interval", type=float, default=900, help="секунд между обходами")
        p.set_defaults(fn=fn)
    p = sub.add_parser("report"); p.add_argument("--top", type=int, default=15)
    p.add_argument("--by-mode", action="store_true", help="сводка по режимам поиска")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("collect", help="набрать базу до --target объявлений по sources.json")
    p.add_argument("--target", type=int, default=2000)
    p.add_argument("--dry-run", action="store_true", help="печатать алерты в консоль (по умолчанию алертов нет)")
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("ui", help="локальный интерфейс с фильтрами: http://127.0.0.1:8765")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("probe"); p.add_argument("url"); p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("deal")
    p.add_argument("action", choices=["buy", "sell", "stats"])
    p.add_argument("ref", nargs="?", help="buy: id объявления; sell: номер сделки")
    p.add_argument("price", nargs="?")
    p.add_argument("--costs"); p.add_argument("--note")
    p.set_defaults(fn=cmd_deal)

    args = ap.parse_args(argv)
    wordlists.ensure_user_copies()   # words/*.txt рядом с базой — их можно править
    return args.fn(args, Config.load(args.env))
