"""CLI: scan | watch | report | probe | deal."""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time

import requests

from . import db
from .collector import AvitoCollector, BlockedError, parse_search_html, read_csv
from .config import Config
from .notifier import Notifier, fmt_rub
from .pipeline import process


def _notifier(cfg: Config, dry: bool) -> Notifier:
    return Notifier("" if dry else cfg.tg_token, "" if dry else cfg.tg_chat_id)


def cmd_scan(args, cfg: Config) -> int:
    conn = db.connect(cfg.db_path)
    notifier = _notifier(cfg, args.dry_run)
    if args.csv:
        stats = process(conn, cfg, read_csv(args.csv), source=f"csv:{args.csv}", complete=False, notifier=notifier)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    urls = [args.url] if args.url else cfg.search_urls
    if not urls:
        print("Нет поисковых ссылок: задайте SEARCH_URLS в .env или --url / --csv", file=sys.stderr)
        return 2
    col = AvitoCollector(cfg.proxy, cfg.delay_sec, cfg.max_pages)
    rc = 0
    for url in urls:
        try:
            items, complete = col.scan(url)
        except BlockedError as e:
            print(f"⛔ {e}. Снизьте частоту, смените IP/прокси или используйте --csv.", file=sys.stderr)
            rc = 3
            continue
        except requests.RequestException as e:
            print(f"⛔ Сеть: {type(e).__name__}", file=sys.stderr)
            rc = 5
            continue
        stats = process(conn, cfg, items, source=url, complete=complete, notifier=notifier)
        print(url, json.dumps(stats, ensure_ascii=False))
    return rc


def cmd_watch(args, cfg: Config) -> int:
    while True:
        rc = cmd_scan(args, cfg)
        pause = args.interval * random.uniform(0.8, 1.3) * (4 if rc == 3 else 1)
        print(f"сплю {pause / 60:.0f} мин")
        time.sleep(pause)


def cmd_report(args, cfg: Config) -> int:
    conn = db.connect(cfg.db_path)
    rows = conn.execute(
        "SELECT title, gpu_key, price, url, net_profit, score_json FROM listings "
        "WHERE status='active' AND net_profit IS NOT NULL ORDER BY net_profit DESC LIMIT ?", (args.top,)
    ).fetchall()
    tot = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    act = conn.execute("SELECT COUNT(*) c FROM listings WHERE status='active'").fetchone()["c"]
    print(f"В базе {tot} объявлений, активных {act}. Кандидатов: {len(rows)}")
    for r in rows:
        sc = json.loads(r["score_json"])
        print(f"- {r['gpu_key']:<18} {fmt_rub(r['price']):>10}  чистыми ≈ +{fmt_rub(r['net_profit'])} "
              f"({sc['net_pct']:.0%}), рынок {fmt_rub(sc['median_ask'])} n={sc['market_n']}\n  {r['url']}")
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
    col = AvitoCollector(cfg.proxy)
    code = 0
    try:
        items = parse_search_html(col.fetch(args.url))
    except BlockedError as e:
        print("⛔", e)
        items, code = [], 3
    except requests.RequestException as e:
        print("⛔ Сеть недоступна:", type(e).__name__, str(e)[:200])
        return 5
    print(f"HTTP {col.last_status} | {len(col.last_html)} байт | title: {_page_title(col.last_html)}")
    if code:
        return code
    print(f"Найдено объявлений: {len(items)}")
    for i in items[:5]:
        print(f"  {i.id} | {i.price:.0f} | {i.title}")
    if not items:
        with open(PROBE_DUMP, "w", encoding="utf-8") as f:
            f.write(col.last_html)
        print(f"0 объявлений: вёрстка изменилась или это заглушка. HTML сохранён в {PROBE_DUMP}")
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
    p = sub.add_parser("report"); p.add_argument("--top", type=int, default=15); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("probe"); p.add_argument("url"); p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("deal")
    p.add_argument("action", choices=["buy", "sell", "stats"])
    p.add_argument("ref", nargs="?", help="buy: id объявления; sell: номер сделки")
    p.add_argument("price", nargs="?")
    p.add_argument("--costs"); p.add_argument("--note")
    p.set_defaults(fn=cmd_deal)

    args = ap.parse_args(argv)
    return args.fn(args, Config.load(args.env))
