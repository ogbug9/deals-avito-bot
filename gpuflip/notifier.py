"""Telegram-оповещения. Без токена работает в режиме печати в консоль."""
from __future__ import annotations

import html

import requests


def fmt_rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def format_alert(title: str, key: str, url: str, sc, flags_info: list[str]) -> str:
    e = html.escape
    lines = [
        f"🔥 <b>{e(key)}</b> — {fmt_rub(sc.ask)}",
        e(title[:120]),
        f"Рынок: медиана {fmt_rub(sc.median_ask)} (n={sc.market_n}), продажа ≈ {fmt_rub(sc.sell_price)}",
        f"Скидка к рынку: {sc.discount_vs_market:.0%}",
        f"Торгуйтесь до ≈ {fmt_rub(sc.target_buy)} → чистыми ≈ <b>+{fmt_rub(sc.net)}</b> ({sc.net_pct:.0%})",
        f"Без торга: {'+' if sc.ask_net >= 0 else ''}{fmt_rub(sc.ask_net)}; оборот ≈ {sc.days_to_sell:.0f} дн.",
    ]
    if sc.flags:
        lines.append("⚠️ Флаги: " + ", ".join(sc.flags))
    if flags_info:
        lines.append("ℹ️ " + ", ".join(flags_info))
    if url:
        lines.append(url)
    return "\n".join(lines)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", out=print, session=None):
        self.token, self.chat_id, self.out = token, chat_id, out
        self.s = session or requests

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            self.out("[dry-run Telegram]\n" + text + "\n")
            return True
        r = self.s.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=20,
        )
        return r.status_code == 200
