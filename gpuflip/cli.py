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
from .collector import AvitoCollector, BlockedError, FetchError, parse_search_html, read_csv, with_page
from .config import Config
from .notifier import Notifier, fmt_rub
from . import modes, wordlists
from .pipeline import process, recheck, rescore


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
                 on_captcha=None, should_stop=lambda: False, limiter=None, info: dict | None = None,
                 do_recheck: bool = True) -> int:
    """todo: (название, ссылка, страниц, категория, регион). target>0 — остановиться, когда в базе столько активных.
    info заполняется итогами: капчи, блокировка, ошибки, пустые выдачи, загружено страниц, новых, кандидатов."""
    rc = 0
    info = info if info is not None else {}
    info.update({"captcha": 0, "blocked": 0, "errors": 0, "empty": 0, "pages": 0, "seen": 0, "new": 0, "candidates": 0})
    with _collector(cfg) as col:
        col.on_captcha = on_captcha
        col.limiter = limiter
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
                info.update(captcha=col.captchas, blocked=1, pages=col.requests)
                return 3   # дальше по списку идти бессмысленно — только продлим блокировку
            except FetchError as e:
                _log_cycle(conn, started, name, captcha=col.captchas - cap0, error=str(e))
                print(f"⛔ Браузер/сеть: {e}", file=sys.stderr, flush=True)
                info["errors"] += 1
                rc = 5
                continue
            stats = process(conn, cfg, items, source=url, complete=complete, notifier=notifier,
                            category=category, region=region, record_alerts=record_alerts)
            _log_cycle(conn, started, name, stats, captcha=col.captchas - cap0)
            info["empty"] += not items            # выдача без объявлений — подозрительный ответ
            for k in ("seen", "new", "candidates"):
                info[k] += stats[k]
            print(f"{name}: {json.dumps(stats, ensure_ascii=False)} | в базе {_active(conn)}", flush=True)
        if (do_recheck and rc == 0 and not target and not should_stop()
                and (cfg.recheck_per_cycle > 0 or cfg.deep_per_cycle > 0)):
            def check(u):
                time.sleep(random.uniform(cfg.delay_sec, cfg.delay_sec * 2))
                return col.check_listing(u)
            res = recheck(conn, cfg, check, notifier=notifier)
            if res["checked"]:
                print(f"Карточки: {json.dumps(res, ensure_ascii=False)}", flush=True)
        info.update(captcha=col.captchas, pages=col.requests)
    return rc


def light_plan(cfg: Config) -> list[tuple]:
    """Лёгкий цикл: только первая страница свежих по приоритетным источникам."""
    return [(s.name + " (свежие)", s.url, 1, s.category, s.region) for s in sources.load() if s.enabled and s.priority]


def run_due(sched, limiter, env_file: str, notifier_factory, on_captcha=None, should_stop=lambda: False,
            log=print, full_interval: float = 0.0) -> tuple[str, int, dict] | None:
    """Выполнить цикл, которому пришло время (полный или лёгкий). None — пока рано."""
    from .schedule import is_trouble
    cfg = Config.load(env_file)
    sched.configure(cfg.light_min_sec, cfg.light_max_sec, full_interval or cfg.full_interval_sec, cfg.light_cycle)
    limiter.max_per_hour = max(1, cfg.max_requests_per_hour)
    kind = sched.due()
    if kind is None:
        return None
    todo = source_plan(cfg) if kind == "full" else light_plan(cfg)
    if not todo:                                   # приоритетных источников нет — лёгкий цикл пропускаем
        sched.done(kind, False)
        return kind, 0, {}
    if kind == "full":
        saved = db.auto_backup(cfg.db_path)
        if saved:
            log(f"Суточная копия базы: {saved}")
    conn = db.connect(cfg.db_path)
    info: dict = {}
    try:
        rc = _run_sources(conn, cfg, notifier_factory(cfg), todo, on_captcha=on_captcha, should_stop=should_stop,
                          limiter=limiter, info=info, do_recheck=kind == "full")
    finally:
        conn.close()
    trouble = is_trouble(rc, info)
    sched.done(kind, trouble)
    if trouble:
        log(f"Защита: замедляюсь в {sched.throttle.factor:.0f} раз(а) — {json.dumps(info, ensure_ascii=False)}")
    return kind, rc, info


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


def _in_category(conn, category: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM listings WHERE status='active' AND category=?", (category,)).fetchone()[0]


def collect_per_category(conn, cfg: Config, target: int, max_page: int = 100) -> int:
    """Добрать каждую категорию до target объявлений. Идёт вглубь выдачи постранично и сохраняет после каждой
    страницы: если Авито закроет доступ, собранное не пропадёт, а повторный запуск продолжит с того же места."""
    srcs = [s for s in sources.load() if s.enabled]
    plan = [(s, conn.execute("SELECT COUNT(*) FROM listings WHERE source=?", (s.url,)).fetchone()[0]) for s in srcs]
    print("Сейчас в базе: " + ", ".join(f"{sources.CATEGORIES[c][0]} {_in_category(conn, c)}"
                                         for c in dict.fromkeys(s.category for s in srcs)), flush=True)
    rc = 0
    with _collector(cfg) as col:
        first = True
        for s, have in plan:
            page = max(1, have // 50)          # с небольшим нахлёстом: за время между обходами выдача сдвигается
            empty = 0
            while _in_category(conn, s.category) < target and page <= max_page:
                if not first:
                    time.sleep(random.uniform(cfg.delay_sec, cfg.delay_sec * 2))
                first = False
                started, cap0 = time.time(), col.captchas
                try:
                    items = parse_search_html(col.fetch(with_page(s.url, page)))
                except BlockedError as e:
                    _log_cycle(conn, started, s.name, captcha=col.captchas - cap0, blocked=1, error=str(e))
                    print(f"⛔ {e}. Остановился на «{s.name}», стр. {page}. Запустите команду ещё раз позже — продолжит.",
                          file=sys.stderr, flush=True)
                    return 3
                except FetchError as e:
                    _log_cycle(conn, started, s.name, error=str(e))
                    print(f"⛔ {s.name}: {e}", file=sys.stderr, flush=True)
                    rc = 5
                    break
                if not items:
                    break                      # выдача кончилась — в этом разделе больше нет
                st = process(conn, cfg, items, source=s.url, complete=False, category=s.category, region=s.region,
                             record_alerts=False)
                _log_cycle(conn, started, f"{s.name}, стр. {page}", st, captcha=col.captchas - cap0)
                print(f"{s.name}: стр. {page}, новых {st['new']} из {st['seen']} | в категории {_in_category(conn, s.category)}",
                      flush=True)
                empty = empty + 1 if st["new"] == 0 else 0
                if empty >= 6 and page > have // 50 + 6:
                    break                      # шесть страниц подряд без новых — дальше только повторы
                page += 1
    print("Итог: " + ", ".join(f"{sources.CATEGORIES[c][0]} {_in_category(conn, c)}"
                                for c in dict.fromkeys(s.category for s in srcs)), flush=True)
    return rc


def cmd_collect(args, cfg: Config) -> int:
    """Первичный набор базы: глубже обычного обхода, пока не наберётся --target объявлений."""
    conn = db.connect(cfg.db_path)
    if args.per_category:
        return collect_per_category(conn, cfg, args.per_category)
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


def cmd_rescore(args, cfg: Config) -> int:
    from . import normalizer
    normalizer.reload_words()
    res = rescore(db.connect(cfg.db_path), cfg)
    print(f"Пересчитано {res['listings']} объявлений, позиций распознано {res['positions']}, кандидатов {res['candidates']}")
    for m, d in res["modes"].items():
        rej = ", ".join(f"{k} {v}" for k, v in sorted(d["rejected"].items(), key=lambda kv: -kv[1])[:6])
        print(f"  {modes.MODE_TITLES[m]:<20} просмотрено {d['viewed']:<5} кандидатов {d['candidates']:<3} отсеяно: {rej}")
    return 0


def cmd_ui(args, cfg: Config) -> int:
    from .ui import serve
    serve(cfg.db_path, args.port, open_browser=not args.no_browser)
    return 0


def cmd_watch(args, cfg: Config) -> int:
    """Постоянный сбор: редкий полный обход + лёгкий цикл по приоритетным источникам, с защитой от блокировок."""
    from .schedule import RateLimiter, Scheduler
    sched = Scheduler(cfg.light_min_sec, cfg.light_max_sec, args.interval or cfg.full_interval_sec, cfg.light_cycle)
    limiter = RateLimiter(cfg.max_requests_per_hour)
    while True:
        res = run_due(sched, limiter, args.env, lambda c: _notifier(c, args.dry_run), full_interval=args.interval)
        if res:
            kind, rc, info = res
            nxt = time.strftime("%H:%M", time.localtime(sched.next_at()))
            print(f"[{time.strftime('%H:%M:%S')}] {'полный обход' if kind == 'full' else 'лёгкий цикл'}: код {rc}, "
                  f"страниц {info.get('pages', 0)}, новых {info.get('new', 0)}, кандидатов {info.get('candidates', 0)}; "
                  f"запросов за час {limiter.used()}/{limiter.max_per_hour}; следующий около {nxt}", flush=True)
        time.sleep(5)


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
            p.add_argument("--interval", type=float, default=0, help="секунд между полными обходами (по умолчанию из .env)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("report"); p.add_argument("--top", type=int, default=15)
    p.add_argument("--by-mode", action="store_true", help="сводка по режимам поиска")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("collect", help="набрать базу до --target объявлений по sources.json")
    p.add_argument("--target", type=int, default=2000)
    p.add_argument("--per-category", type=int, default=0, metavar="N", help="добрать каждую категорию до N объявлений")
    p.add_argument("--dry-run", action="store_true", help="печатать алерты в консоль (по умолчанию алертов нет)")
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("rescore", help="пересчитать базу текущими правилами (после правки words/*.txt), без сети")
    p.set_defaults(fn=cmd_rescore)
    p = sub.add_parser("app", help="десктопное приложение: окно, трей и фоновый сбор")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=None)
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

    raw = sys.argv[1:] if argv is None else list(argv)
    if raw[:1] == ["app"]:                      # у приложения свои параметры и своя домашняя папка
        from .app import main as app_main
        return app_main(raw[1:])
    args = ap.parse_args(argv)
    wordlists.ensure_user_copies()   # words/*.txt рядом с базой — их можно править
    return args.fn(args, Config.load(args.env))
