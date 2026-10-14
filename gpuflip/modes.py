"""Режимы поиска перекосов цены. Только оценка и объяснение: ничего не покупает и никому не пишет.

market      — классика: модель из заголовка, цена ниже медианы (всегда включён)
bundles     — 1: целый ПК дешевле суммы своих деталей
regional    — 2: лот в регионе с Авито Доставкой дешевле московской медианы
badtext     — 3: модель найдена только в описании или с исправлением опечатки
urgent      — 4: срочная продажа — порог прибыли ниже
categories  — 5: ОЗУ, процессоры, БП, диски, платы
manual      — «проверить вручную»: модель не определилась, а цена подозрительно низкая (без алерта)
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field

from . import db, parts
from .config import Config
from .notifier import fmt_rub
from .scoring import Score, price_points, score_listing

MODE_TITLES = {
    "market": "ниже рынка", "bundles": "разбор ПК", "regional": "регион → Москва", "badtext": "плохое описание",
    "urgent": "срочная продажа", "categories": "комплектующие", "manual": "проверить вручную",
}
ALERT_MODES = ("market", "bundles", "regional", "badtext", "urgent", "categories")   # manual — только в список
CONFIDENCE_RU = {"high": "высокое", "medium": "среднее", "low": "низкое"}
PART_RU = {"gpu": "видеокарта", "cpu": "процессор", "ram": "ОЗУ", "mb": "плата", "psu": "БП", "disk": "диск", "case": "корпус"}
GPU_CATEGORIES = ("", "videokarty")
PART_SELL_COST = 150.0     # упаковка/встреча на каждую деталь при продаже ПК по частям


@dataclass
class Cand:
    mode: str
    est_profit: float
    est_profit_pct: float
    reason: str
    confidence: str = "high"


@dataclass
class ModeLog:
    """Счётчики одного обхода: сколько просмотрено, отсеяно (по причинам) и найдено."""
    viewed: Counter = field(default_factory=Counter)
    cands: Counter = field(default_factory=Counter)
    rejected: dict = field(default_factory=dict)

    def view(self, mode: str) -> None:
        self.viewed[mode] += 1

    def reject(self, mode: str, reason: str) -> None:
        self.rejected.setdefault(mode, Counter())[reason.split("(")[0]] += 1

    def hit(self, mode: str) -> None:
        self.cands[mode] += 1

    def summary(self) -> dict:
        return {m: {"viewed": self.viewed[m], "candidates": self.cands[m], "rejected": dict(self.rejected.get(m, {}))}
                for m in MODE_TITLES if self.viewed[m]}

    def save(self, conn, source: str, now: float) -> None:
        for m, d in self.summary().items():
            conn.execute("INSERT INTO mode_stats VALUES(?,?,?,?,?,?)",
                         (now, source, m, d["viewed"], d["candidates"], json.dumps(d["rejected"], ensure_ascii=False)))


def enabled(cfg: Config, mode: str) -> bool:
    return mode in ("market", "manual") or getattr(cfg, f"mode_{mode}", False)


def _economics(price: float, sell_price: float, flags: dict, cfg: Config, days: float, extra_cost: float = 0.0) -> tuple[float, float, float]:
    """(цель покупки после торга, чистая прибыль, доля от вложенного). sell_price — цена быстрой продажи."""
    buy = price * (1 - cfg.negotiation_discount)
    sell = sell_price * (1 - cfg.sell_fee_pct)
    capital = buy * cfg.capital_cost_per_day * days
    risk = min(0.6, sum(flags.values())) * buy * cfg.risk_loss_share
    net = sell - buy - cfg.fixed_costs - extra_cost - capital - risk
    return buy, net, (net / buy if buy else 0.0)


def _risk_note(flags: dict) -> str:
    return f"; штраф за риск: {', '.join(sorted(flags))}" if flags else ""


def _market_note(ms: db.MarketStats) -> str:
    return f"медиана {fmt_rub(ms.median_ask)} (n={ms.n})"


def evaluate(conn, cfg: Config, row, sc: Score | None, ms: db.MarketStats | None, now: float, log: ModeLog,
             ceilings: dict | None = None) -> list[Cand]:
    """row — запись listings после нормализации; sc/ms — классическая оценка и рынок позиции (если есть)."""
    out: list[Cand] = []
    flags = json.loads(row["flags"] or "{}")
    is_gpu = row["category"] in GPU_CATEGORIES
    key = row["item_key"]

    if row["defect"] or row["wanted"] or row["junk"]:
        mode = "market" if is_gpu else ("bundles" if row["kind"] == "pc" else "categories")
        if enabled(cfg, mode):
            log.view(mode)
            log.reject(mode, "defective" if row["defect"] else "wanted" if row["wanted"] else "junk")
        return out

    # --- 1. целый ПК ---
    if row["kind"] == "pc":
        if enabled(cfg, "bundles"):
            c = _bundle(conn, cfg, row, flags, now, log)
            if c:
                out.append(c)
        return out

    # --- видеокарта без модели → «проверить вручную» ---
    if is_gpu and not key:
        log.view("market")
        log.reject("market", row["skip_reason"] or "no_model")
        c = _manual(conn, cfg, row, now, log)
        return [c] if c else out

    if not key or sc is None or ms is None:
        if not is_gpu and enabled(cfg, "categories"):
            log.view("categories")
            log.reject("categories", "position_not_recognized")
        return out

    source = row["model_source"] or "title"
    below_median = ms.median_ask > 0 and row["price"] < ms.median_ask
    base = (f"{ms.key or key}: {fmt_rub(row['price'])} при рынке {_market_note(ms)}, "
            f"быстрая продажа ≈ {fmt_rub(sc.quick_price)} ({sc.basis})")

    # --- классика / 5. комплектующие / 3. плохое описание: одна формула, разные условия допуска ---
    mode = "market" if is_gpu else "categories"
    if is_gpu and source != "title":
        mode = "badtext"
    kind = key.split(" ")[0]
    if enabled(cfg, mode):
        log.view(mode)
        if not sc.ok:
            log.reject(mode, sc.reason)
        elif mode == "badtext" and source == "fuzzy" and row["price"] > ms.median_ask * cfg.fuzzy_max_ratio:
            log.reject(mode, "fuzzy_price_above_60pct")
        else:
            how = {"description": "модель найдена только в описании", "fuzzy": "модель восстановлена из опечатки"}
            conf = {"title": "high", "description": "medium", "fuzzy": "low"}[source]
            unit = f", {row['unit_price']:.0f} {_unit(row)}" if row["unit_price"] else ""
            coarse = ""
            if kind in ("RAM", "PSU", "SSD", "HDD", "MB") and not sc.precise:
                # общая позиция не различает бренд и модель — человек должен сверить; при точной позиции доверие высокое
                conf, coarse = "medium", "; позиция без учёта бренда — сверьте вручную"
            if row["reseller"]:
                coarse += "; у продавца много одинаковых лотов"
            # «высокое» доверие — про лот, а не только про данные: риск в тексте или пустое описание его снижают
            if conf == "high" and any(f != "model_from_desc" for f in flags):
                conf = "medium"
            if not (row["description"] or "").strip():
                conf = "medium" if conf == "high" else conf
                coarse += "; описания нет — состояние неизвестно"
            reason = (f"{how[source]} — " if mode == "badtext" else "") + \
                     f"{base}{unit}; торг до {fmt_rub(sc.target_buy)}{coarse}{_risk_note(flags)}"
            out.append(Cand(mode, sc.net, sc.net_pct, reason, conf))

    # --- 4. срочная продажа: тот же расчёт, порог прибыли ниже ---
    if row["urgent"] and enabled(cfg, "urgent"):
        log.view("urgent")
        if ms.n < cfg.alert_market_n:
            log.reject("urgent", "low_data" if ms.n >= cfg.min_market_n else "thin_market")
        elif not below_median:
            log.reject("urgent", "not_below_median")
        elif sc.reason not in ("ok", "profit_too_small", "margin_too_small"):
            log.reject("urgent", sc.reason)
        elif sc.net < cfg.min_profit_rub or sc.net_pct < cfg.urgent_profit_pct:
            log.reject("urgent", "profit_below_urgent_threshold")
        elif source == "fuzzy" and row["price"] > ms.median_ask * cfg.fuzzy_max_ratio:
            log.reject("urgent", "fuzzy_price_above_60pct")
        else:
            out.append(Cand("urgent", sc.net, sc.net_pct,
                            f"срочная продажа — {base}; порог прибыли снижен до {cfg.urgent_profit_pct:.0%}{_risk_note(flags)}",
                            "high" if source == "title" else "low" if source == "fuzzy" else "medium"))

    # --- 2. региональный арбитраж ---
    if enabled(cfg, "regional") and not row["is_msk"]:
        c = _regional(conn, cfg, row, flags, ms, now, log, (ceilings or {}).get(key))
        if c:
            out.append(c)
    return out


def _unit(row) -> str:
    return {"RAM": "₽/ГБ", "SSD": "₽/ГБ", "HDD": "₽/ГБ", "PSU": "₽/Вт"}.get((row["item_key"] or "").split(" ")[0], "")


def _where(row) -> str:
    from .sources import REGIONS
    if row["city"]:
        return row["city"] if row["city"].startswith("в ") else f"из «{row['city']}»"
    if row["region"] and row["region"] != "all":
        return f"({REGIONS.get(row['region'], row['region'])})"
    return "(город в выдаче не указан)"


def _regional(conn, cfg: Config, row, flags: dict, ms_all: db.MarketStats, now: float, log: ModeLog,
              ceiling: tuple | None = None) -> Cand | None:
    log.view("regional")
    if not row["delivery"]:
        return log.reject("regional", "no_delivery")
    if row["price"] < cfg.min_price or row["price"] > cfg.max_price:
        return log.reject("regional", "out_of_budget")
    if "prepay" in flags:
        return log.reject("regional", "prepayment")
    msk = db.market_stats(conn, row["item_key"], row["id"], now, msk=True)
    ref, where = (msk, "по Москве") if msk.n >= cfg.min_market_n else (ms_all, "общая: по Москве мало данных")
    if ref.n < cfg.min_market_n:
        return log.reject("regional", "thin_market")
    if row["price"] >= ref.median_ask:
        return log.reject("regional", "not_below_median")
    if row["price"] < ref.median_ask * cfg.scam_price_ratio:
        return log.reject("regional", "too_cheap_scam_risk")
    pts = price_points(ref, cfg, ceiling)
    buy, net, pct = _economics(row["price"], pts.quick, flags, cfg, ref.days_to_sell or cfg.default_days_to_sell,
                               cfg.delivery_cost)
    if net < cfg.min_profit_rub or pct < cfg.min_profit_pct:
        return log.reject("regional", "profit_too_small")
    if ref.n < cfg.alert_market_n:
        return log.reject("regional", "low_data")
    local = db.market_stats(conn, row["item_key"], row["id"], now, msk=False)
    local_note = f", в регионах {_market_note(local)}" if local.n >= cfg.min_market_n else ""
    return Cand("regional", net, pct,
                f"{row['item_key']}: {fmt_rub(row['price'])} {_where(row)} с Авито Доставкой; "
                f"московская {_market_note(ref)} ({where}){local_note}; быстрая продажа ≈ {fmt_rub(pts.quick)}; "
                f"доставка {fmt_rub(cfg.delivery_cost)}, "
                f"торг до {fmt_rub(buy)}{_risk_note(flags)}",
                "high" if where == "по Москве" else "medium")


def _bundle(conn, cfg: Config, row, flags: dict, now: float, log: ModeLog) -> Cand | None:
    log.view("bundles")
    b = parts.parse_pc(row["title"], row["description"] or "")
    if b.reject:
        return log.reject("bundles", b.reject)
    if row["price"] < cfg.min_price or row["price"] > cfg.max_price:
        return log.reject("bundles", "out_of_budget")
    if "prepay" in flags:
        return log.reject("bundles", "prepayment")
    priced: list[tuple[parts.Part, float, int]] = []
    unknown = list(b.unknown)
    for part in b.parts:
        ms = db.market_stats(conn, part.key, row["id"], now)
        if ms.n >= cfg.min_market_n:
            priced.append((part, ms.median_ask, ms.n))
        else:
            unknown.append(f"{part.key} (нет рынка)")       # деталь узнали, но цену не выдумываем
    if not any(p.kind == "gpu" for p, _, _ in priced):
        return log.reject("bundles", "gpu_absent" if "gpu" in b.absent else "gpu_not_priced")
    if len(priced) < 2:
        return log.reject("bundles", "only_gpu_recognized")
    total = sum(m for _, m, _ in priced)
    if total < row["price"] * (1 + cfg.bundle_min_markup):
        return log.reject("bundles", "parts_not_25pct_above_price")
    buy, net, pct = _economics(row["price"], total * cfg.ask_to_sale, flags, cfg, cfg.bundle_days,
                               PART_SELL_COST * len(priced))
    if net < cfg.min_profit_rub or pct < cfg.min_profit_pct:
        return log.reject("bundles", "profit_too_small")
    if min(n for _, _, n in priced) < cfg.alert_market_n:
        return log.reject("bundles", "low_data")
    n_unknown = len([u for u in unknown if u != "case"])
    conf = "high" if n_unknown == 0 else "medium" if n_unknown <= 2 else "low"
    listed = " + ".join(f"{p.key} {fmt_rub(m)}" for p, m, _ in priced)
    unk = ", ".join(PART_RU.get(u, u) for u in unknown)
    absent = f"; без: {', '.join(PART_RU.get(a, a) for a in b.absent)}" if b.absent else ""
    return Cand("bundles", net, pct,
                f"ПК за {fmt_rub(row['price'])}, детали по медианам: {listed} = {fmt_rub(total)}; не оценено: {unk}{absent}; "
                f"продажа по частям ≈ {cfg.bundle_days:.0f} дн.{_risk_note(flags)}", conf)


def _manual(conn, cfg: Config, row, now: float, log: ModeLog) -> Cand | None:
    """Модель не определилась, а цена заметно ниже обычной для раздела — человеку стоит взглянуть."""
    if row["skip_reason"] not in ("model_not_found", "vram_unknown") or not row["price"] or row["price"] < cfg.min_price:
        return None
    log.view("manual")
    prices = [r[0] for r in conn.execute(
        "SELECT price FROM listings WHERE category=? AND status='active' AND item_key IS NOT NULL AND defect=0 AND price>0",
        (row["category"],))]
    if len(prices) < 30:
        return log.reject("manual", "thin_market")
    low = statistics.quantiles(prices, n=4)[0]
    if row["price"] > low * 0.6:
        return log.reject("manual", "price_not_suspicious")
    return Cand("manual", 0.0, 0.0,
                f"модель не определена, цена {fmt_rub(row['price'])} ниже 60% нижнего квартиля раздела ({fmt_rub(low)})", "low")


def format_alert(row, cands: list[Cand]) -> str:
    import html
    e = html.escape
    best = max(cands, key=lambda c: c.est_profit)
    tags = ", ".join(MODE_TITLES[c.mode] for c in cands)
    lines = [f"🔥 <b>{e(row['item_key'] or row['title'][:60])}</b> — {fmt_rub(row['price'])}", e(row["title"][:120])]
    sc = json.loads(row["score_json"]) if row["score_json"] else None
    if sc and sc.get("market_n"):
        lines.append(f"Рынок: {fmt_rub(sc['median_ask'])}, n={sc['market_n']}"
                     + (f", скидка {sc['discount_vs_market']:.0%}" if sc.get("discount_vs_market") else ""))
    lines.append(f"Режим: {tags}")
    lines += [f"• {e(c.reason)}" for c in cands]
    lines.append(f"Чистыми ≈ <b>+{fmt_rub(best.est_profit)}</b> ({best.est_profit_pct:.0%}), доверие: {CONFIDENCE_RU[best.confidence]}")
    if row["url"]:
        lines.append(row["url"])
    return "\n".join(lines)


def store(conn, row, cands: list[Cand], now: float) -> None:
    """Записывает кандидатов по режимам и сводку в строку объявления; снимает устаревшие теги."""
    lid = row["id"]
    risky = any(f != "model_from_desc" for f in json.loads(row["flags"] or "{}"))
    if risky or not (row["description"] or "").strip():     # для всех режимов: риск в тексте или нет описания — не «высокое»
        for c in cands:
            if c.confidence == "high":
                c.confidence = "medium"
    modes = [c.mode for c in cands]
    # что перестало быть кандидатом (цена выросла, правила изменились) — убираем; проверенные карточкой остаются для отчёта
    keep = ",".join("?" * len(modes)) or "''"
    conn.execute(f"DELETE FROM candidates WHERE listing_id=? AND checked_at IS NULL AND mode NOT IN ({keep})", [lid] + modes)
    for c in cands:
        conn.execute(
            "INSERT INTO candidates(listing_id,mode,first_at,last_at,price,est_profit,est_profit_pct,reason,confidence) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(listing_id,mode) DO UPDATE SET last_at=excluded.last_at, "
            "price=excluded.price, est_profit=excluded.est_profit, est_profit_pct=excluded.est_profit_pct, "
            "reason=excluded.reason, confidence=excluded.confidence",
            (lid, c.mode, now, now, row["price"], c.est_profit, c.est_profit_pct, c.reason, c.confidence))
    if cands:
        best = max(cands, key=lambda c: c.est_profit)
        conn.execute("UPDATE listings SET mode_tags=?, est_profit=?, est_profit_pct=?, reason=?, confidence=? WHERE id=?",
                     (",".join(modes), best.est_profit, best.est_profit_pct, " | ".join(c.reason for c in cands),
                      best.confidence, lid))
    else:
        conn.execute("UPDATE listings SET mode_tags=NULL, est_profit=NULL, est_profit_pct=NULL, reason=NULL, confidence=NULL "
                     "WHERE id=? AND mode_tags IS NOT NULL", (lid,))


def by_mode_report(conn) -> list[dict]:
    """Основная метрика недели теста: по каждому режиму — просмотрено, кандидатов, средняя прибыль, доля ушедших."""
    out = []
    for mode, title in MODE_TITLES.items():
        viewed = conn.execute("SELECT COALESCE(SUM(viewed),0) FROM mode_stats WHERE mode=?", (mode,)).fetchone()[0]
        rej: Counter = Counter()
        for (j,) in conn.execute("SELECT rejected FROM mode_stats WHERE mode=?", (mode,)):
            rej.update(json.loads(j or "{}"))
        n, avg, checked, inactive = conn.execute(
            "SELECT COUNT(*), AVG(est_profit), SUM(checked_at IS NOT NULL), SUM(inactive_at IS NOT NULL) "
            "FROM candidates WHERE mode=?", (mode,)).fetchone()
        out.append({"mode": mode, "title": title, "viewed": viewed, "candidates": n, "avg_profit": avg or 0.0,
                    "checked": checked or 0, "inactive": inactive or 0,
                    "inactive_share": (inactive / checked) if checked else None, "rejected": dict(rej.most_common(6))})
    return out
