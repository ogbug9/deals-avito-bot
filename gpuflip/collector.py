"""Сборщик объявлений: Avito (best effort) и импорт из CSV.

Авито активно блокирует ботов (IP-бан, капча) и запрещает автоматический сбор условиями
использования. Поэтому: умеренный темп, прокси по желанию, при блокировке — остановка, а
запасной путь — CSV-импорт (выгрузка из браузерного расширения / ручная).
"""
from __future__ import annotations

import csv
import hashlib
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.avito.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


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


class AvitoCollector:
    def __init__(self, proxy: str = "", delay: float = 6.0, max_pages: int = 5, session=None, sleep=time.sleep):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.delay, self.max_pages, self._sleep = delay, max_pages, sleep

    def fetch(self, url: str) -> str:
        r = self.s.get(url, timeout=30)
        if is_blocked(r.status_code, r.text):
            raise BlockedError(f"HTTP {r.status_code}: Авито блокирует запросы (IP/капча)")
        r.raise_for_status()
        return r.text

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
