"""Сборщик объявлений: Avito через настоящий Chrome (Playwright) и импорт из CSV.

Авито активно блокирует ботов (IP-бан, капча) и запрещает автоматический сбор условиями
использования; голый requests получает 429. Поэтому: реальный браузер с постоянным профилем,
умеренный темп, прокси по желанию, капчу проходит человек, при блокировке — остановка, а
запасной путь — CSV-импорт (выгрузка из браузерного расширения / ручная).
"""
from __future__ import annotations

import csv
import hashlib
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

BASE = "https://www.avito.ru"
ITEM_MARK = 'data-marker="item"'


@dataclass
class RawListing:
    id: str
    title: str
    price: float
    url: str = ""
    description: str = ""
    city: str = ""


class BlockedError(RuntimeError):
    pass


class FetchError(RuntimeError):
    """Браузер не запустился или страница не открылась (сеть, прокси, нет Chrome)."""


def _digits(s: str) -> float:
    d = re.sub(r"[^\d]", "", s or "")
    return float(d) if d else 0.0


def parse_search_html(html: str) -> list[RawListing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawListing] = []
    for it in soup.select('[data-marker="item"]'):
        iid = it.get("data-item-id")
        a = it.select_one('[data-marker="item-title"]') or it.select_one('a[itemprop="url"]')
        if not iid or not a:
            continue
        href = a.get("href", "")
        url = href if href.startswith("http") else BASE + href
        title_el = it.select_one('[itemprop="name"]') or a
        title = title_el.get_text(" ", strip=True)
        meta = it.select_one('meta[itemprop="price"]')
        price = _digits(meta.get("content", "")) if meta else 0.0
        if not price:
            pe = it.select_one('[data-marker="item-price"]')
            price = _digits(pe.get_text()) if pe else 0.0
        de = it.select_one('[class*="description"]')
        ce = it.select_one('[data-marker="item-address"]') or it.select_one('[class*="geo-root"]')
        out.append(RawListing(
            id=str(iid), title=title, price=price, url=url,
            description=de.get_text(" ", strip=True) if de else "",
            city=ce.get_text(" ", strip=True) if ce else "",
        ))
    return out


def is_blocked(status: int, html: str) -> bool:
    if status in (403, 429, 407):
        return True
    low = html[:5000].lower()
    return any(w in low for w in ("проблема с ip", "доступ ограничен", "captcha", "firewall"))


def with_page(url: str, page: int) -> str:
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    q["p"] = str(page)
    return urlunparse(u._replace(query=urlencode(q)))


def _proxy_settings(proxy: str) -> dict | None:
    """http://user:pass@host:port → формат Playwright."""
    if not proxy:
        return None
    u = urlparse(proxy)
    out = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username:
        out["username"], out["password"] = unquote(u.username), unquote(u.password or "")
    return out


class AvitoCollector:
    """Обход поиска через настоящий Chrome (Playwright, channel="chrome").

    Профиль постоянный (profile_dir): куки и пройденная капча живут между запусками.
    Капчу код не решает: в оконном режиме ждёт до captcha_wait секунд, пока её пройдёт человек.
    """

    def __init__(self, proxy: str = "", delay: float = 6.0, max_pages: int = 5, profile_dir: str = ".chrome-profile",
                 headless: bool = False, captcha_wait: float = 180.0, sleep=time.sleep):
        self.proxy, self.profile_dir, self.headless, self.captcha_wait = proxy, profile_dir, headless, captcha_wait
        self.delay, self.max_pages, self._sleep = delay, max_pages, sleep
        self.last_status, self.last_html = 0, ""   # для диагностики (probe)
        self._pw = self._ctx = self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _start(self):
        from playwright.sync_api import sync_playwright   # тяжёлый импорт — только когда нужен браузер
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir, channel="chrome", headless=self.headless, locale="ru-RU",
            proxy=_proxy_settings(self.proxy), viewport={"width": 1280, "height": 900})
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self):
        for obj, fn in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:
                pass
        self._pw = self._ctx = self._page = None

    def _snapshot(self) -> bool:
        """Обновляет last_html; True, если на странице есть объявления."""
        self.last_html = self._page.content()
        return ITEM_MARK in self.last_html

    def fetch(self, url: str) -> str:
        try:
            if not self._page:
                self._start()
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            raise FetchError(f"{type(e).__name__}: {str(e).splitlines()[0][:200]}") from e
        self.last_status = resp.status if resp else 0
        try:
            self._page.wait_for_selector('[data-marker="item"]', timeout=15000)
        except Exception:
            pass
        if self._snapshot():
            return self.last_html
        if is_blocked(self.last_status, self.last_html):
            if not self.headless and self.captcha_wait > 0:
                print(f"⏸ Авито просит капчу. Пройдите её в открытом окне Chrome (жду до {self.captcha_wait:.0f} с)…",
                      flush=True)
                deadline = time.time() + self.captcha_wait
                while time.time() < deadline:
                    self._page.wait_for_timeout(2000)
                    if self._snapshot():
                        self.last_status = 200
                        return self.last_html
            raise BlockedError(f"HTTP {self.last_status}: Авито блокирует запросы (IP/капча)")
        return self.last_html

    def scan(self, url: str) -> tuple[list[RawListing], bool]:
        """Возвращает (объявления, полный_ли_обход). Полный — дошли до последней страницы."""
        seen: dict[str, RawListing] = {}
        complete = False
        prev_ids: set[str] = set()
        for page in range(1, self.max_pages + 1):
            items = parse_search_html(self.fetch(with_page(url, page)))
            ids = {i.id for i in items}
            if not items or ids <= prev_ids:   # пусто или Авито отдаёт ту же последнюю страницу
                complete = True
                break
            for i in items:
                seen[i.id] = i
            prev_ids = ids
            if page < self.max_pages:
                self._sleep(random.uniform(self.delay, self.delay * 2))
        return list(seen.values()), complete


def read_csv(path: str) -> list[RawListing]:
    """CSV: колонки title, price обязательны; id, url, description, city — по желанию."""
    out: list[RawListing] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            title = (row.get("title") or "").strip()
            if not title:
                continue
            url = (row.get("url") or "").strip()
            iid = (row.get("id") or "").strip() or hashlib.sha1((url or title).encode()).hexdigest()[:12]
            out.append(RawListing(id=iid, title=title, price=_digits(row.get("price", "")), url=url,
                                  description=(row.get("description") or "").strip(),
                                  city=(row.get("city") or "").strip()))
    return out
