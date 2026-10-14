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
import sys
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
    delivery: bool = False        # значок «Авито Доставка» в карточке
    image: str = ""
    date_text: str = ""           # «2 часа назад»
    rating: float | None = None   # рейтинг продавца
    reviews: int | None = None
    seller_id: str = ""           # код продавца из ссылки /brands/… или /user/… (есть не у всех)
    seller_name: str = ""

    @property
    def age(self) -> float | None:
        return age_seconds(self.date_text)


class BlockedError(RuntimeError):
    pass


class FetchError(RuntimeError):
    """Браузер не запустился или страница не открылась (сеть, прокси, нет Chrome)."""


class BusyError(FetchError):
    """Сборщик уже занят другим процессом (приложение или collect) — второй Chrome не запускаем."""


def _digits(s: str) -> float:
    d = re.sub(r"[^\d]", "", s or "")
    return float(d) if d else 0.0


def _description(it) -> str:
    """Текст объявления в карточке. В вёрстке 2026 у него нет своего маркера: это <p> перед датой."""
    de = it.select_one('[class*="description"]')
    if de:
        return de.get_text(" ", strip=True)
    date = it.select_one('[data-marker="item-date/wrapper"]') or it.select_one('[data-marker="item-date"]')
    p = date.find_previous("p") if date else None
    if (p is None or it not in p.parents or p.get("data-marker")
            or p.find_parent(attrs={"data-marker": ("item-location", "item-price")})):
        return ""
    text = p.get_text(" ", strip=True)
    return "" if re.match(r"Доставка от \d", text) else text      # это плашка доставки, а не описание


_AGE_UNITS = (("минут", 60), ("час", 3600), ("дн", 86400), ("день", 86400), ("недел", 7 * 86400), ("месяц", 30 * 86400))


def age_seconds(date_text: str) -> float | None:
    """«2 часа назад» → 7200. None, если формат незнакомый."""
    m = re.search(r"(\d+)\s+([а-я]+)", (date_text or "").lower())
    if not m:
        return None
    for stem, sec in _AGE_UNITS:
        if m.group(2).startswith(stem):
            return int(m.group(1)) * sec
    return None


def _text(it, selector: str) -> str:
    el = it.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _image(it) -> str:
    li = it.select_one('[data-marker^="slider-image/image-"]')   # картинки грузятся лениво, адрес есть в маркере
    if li:
        return li["data-marker"].split("image-", 1)[1]
    img = it.select_one('img[itemprop="image"]')
    return img.get("src", "") if img else ""


_SELLER_HREF_RE = re.compile(r"/(?:brands|user)/([0-9a-zA-Z_\-]{6,})")


def _seller(it) -> tuple[str, str]:
    """(код, имя) продавца из карточки выдачи. У частных продавцов без рейтинга блока нет — вернётся ('', '')."""
    sid = ""
    for a in it.select('a[href*="/brands/"], a[href*="/user/"]'):
        m = _SELLER_HREF_RE.search(a.get("href", ""))
        if m:
            sid = m.group(1)
            break
    anchor = it.select_one('[data-marker="seller-rating"]') or it.select_one('[data-marker="seller-info/score"]')
    p = anchor.find_previous("p") if anchor else None
    name = p.get_text(" ", strip=True) if p is not None and it in p.parents and not p.get("data-marker") else ""
    return sid, name[:80]


def parse_search_html(html: str) -> list[RawListing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawListing] = []
    for it in soup.select('[data-marker="item"]'):
        iid = it.get("data-item-id")
        a = it.select_one('[data-marker="item-title"]') or it.select_one('a[itemprop="url"]')
        if not iid or not a:
            continue
        href = a.get("href", "").split("?")[0]   # ?context=… — мусор сессии, для ссылки не нужен
        url = href if href.startswith("http") else BASE + href
        title_el = it.select_one('[itemprop="name"]') or a
        title = title_el.get_text(" ", strip=True)
        meta = it.select_one('meta[itemprop="price"]')
        price = _digits(meta.get("content", "")) if meta else 0.0
        if not price:
            pe = it.select_one('[data-marker="item-price"]')
            price = _digits(pe.get_text()) if pe else 0.0
        ce = (it.select_one('[data-marker="item-location"]') or it.select_one('[data-marker="item-address"]')
              or it.select_one('[class*="geo-root"]'))
        rating = _text(it, '[data-marker="seller-rating/score"]').replace(",", ".")
        seller_id, seller_name = _seller(it)
        reviews = _digits(_text(it, '[data-marker="seller-info/summary"]'))
        out.append(RawListing(
            id=str(iid), title=title, price=price, url=url,
            description=_description(it),
            city=re.sub(r"\s+,", ",", ce.get_text(" ", strip=True)) if ce else "",
            delivery=it.select_one('svg[data-icon-name="delivery"]') is not None,
            image=_image(it),
            date_text=_text(it, '[data-marker="item-date"]'),
            rating=float(rating) if re.fullmatch(r"\d(\.\d+)?", rating) else None,
            reviews=int(reviews) if reviews else None,
            seller_id=seller_id, seller_name=seller_name,
        ))
    return out


_CLOSED_RE = re.compile(r"снято с публикации|объявление снято|объявление удалено|объявление закрыто|"
                        r"срок размещения .{0,20}ист[её]к|такой страницы не существует|страница не найдена", re.I)
_TITLE_CITY_RE = re.compile(r"купить в ([А-ЯЁ][^|]{1,40}?)(?= по низкой| с доставкой| \||$)")
_CONDITION_RE = re.compile(r"Состояние\s*:?\s*(Новое|Как новое|Отличное|Хорошее|Удовлетворительное|Б/у)", re.I)


def parse_listing_page(status: int, html: str) -> dict:
    """Страница карточки → жива ли она. «unknown» — капча или незнакомая вёрстка: статус не меняем."""
    if status in (404, 410):
        return {"state": "inactive"}
    if is_blocked(status, html):
        return {"state": "unknown"}
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('[data-marker*="closed-warning"]') or _CLOSED_RE.search(soup.get_text(" ", strip=True)[:6000]):
        return {"state": "inactive"}
    if not (soup.select_one('[data-marker^="item-view/"]') or soup.select_one('[itemprop="price"]')):
        return {"state": "unknown"}
    de = soup.select_one('[data-marker="item-view/item-description"]') or soup.select_one('[itemprop="description"]')
    cond = _CONDITION_RE.search(soup.get_text(" ", strip=True))
    # в общероссийской выдаче города нет — он есть только в заголовке карточки: «… купить в Зеленодольске …»
    city = _TITLE_CITY_RE.search(soup.title.get_text(" ", strip=True)) if soup.title else None
    out = {"state": "active", "description": de.get_text(" ", strip=True) if de else "",
           "condition": cond.group(1) if cond else "", "city": f"в {city.group(1)}" if city else ""}
    out.update(_card_details(soup))
    return out


def _card_details(soup) -> dict:
    """Глубокая карточка: фото, продавец, характеристики. Чего на странице нет — того и в ответе нет."""
    d: dict = {}
    gal = soup.select_one('[data-marker="item-view/main-gallery"]') or soup.select_one('[data-marker="item-view/gallery"]')
    photos: list[str] = []
    for img in (gal.select("img") if gal else []):
        src = img.get("src") or ""
        if src.startswith("https://") and src not in photos:
            photos.append(src)
    if photos:
        d["photos"] = photos[:12]
    name = soup.select_one('[data-marker="seller-info/name"]')
    if name:
        d["seller_name"] = name.get_text(" ", strip=True)[:80]
    label = soup.select_one('[data-marker="seller-info/label"]')
    if label:
        d["seller_type"] = label.get_text(" ", strip=True)[:40]
    rate = soup.select_one('[data-marker="sellerRate"] meta[itemprop="ratingValue"]')
    if rate and re.fullmatch(r"\d(\.\d+)?", rate.get("content", "")):
        d["rating"] = float(rate["content"])
    params = soup.select_one('[data-marker="item-view/item-params"]')
    if params:
        text = params.get_text(" ", strip=True)
        brand = re.search(r"Бренд\s*:\s*([^:]+?)(?=\s+[А-ЯЁ][а-яё ]+\s*:|$)", text)
        model = re.search(r"Модель\s*:\s*([^:]+?)(?=\s+[А-ЯЁ][а-яё ]+\s*:|$)", text)
        am = " ".join(x.group(1).strip() for x in (brand, model) if x)
        if am:
            d["avito_model"] = am[:120]
    return d


def is_blocked(status: int, html: str) -> bool:
    if 400 <= status < 500 and status not in (404, 410):   # 403/429, а также нестандартные вроде 439
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
        self.delay_max = 0.0
        self.who = " ".join(sys.argv[1:3]) or "gpuflip"
        self.last_status, self.last_html = 0, ""   # для диагностики (probe)
        self.captchas = 0             # сколько раз за сеанс Авито просил капчу
        self.requests = 0             # сколько страниц загружено за сеанс
        self.limiter = None           # schedule.RateLimiter: потолок загрузок в час
        self.on_captcha = None        # callback(): приложение показывает уведомление
        self._pw = self._ctx = self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _start(self):
        from . import guard
        guard.acquire(self.who)                            # один Chrome на профиль: второй процесс получит BusyError
        from playwright.sync_api import sync_playwright   # тяжёлый импорт — только когда нужен браузер
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir, channel="chrome", headless=self.headless, locale="ru-RU",
            proxy=_proxy_settings(self.proxy), viewport={"width": 1280, "height": 900})
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self):
        started = self._pw is not None
        for obj, fn in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:
                pass
        self._pw = self._ctx = self._page = None
        if started:
            from . import guard
            guard.release()

    def _count(self) -> None:
        """Перед каждой загрузкой: учесть запрос и, если упёрлись в часовой лимит, подождать."""
        if self.limiter is not None:
            self.limiter.acquire()
        self.requests += 1

    def _snapshot(self) -> bool:
        """Обновляет last_html; True, если на странице есть объявления."""
        for _ in range(8):
            try:
                self.last_html = self._page.content()
                break
            except Exception:              # страница в этот момент переходит (редирект после проверки) — ждём
                self._page.wait_for_timeout(1500)
        return ITEM_MARK in self.last_html

    def fetch(self, url: str) -> str:
        html = self._fetch(url)
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        if slug and slug not in urlparse(self._page.url).path:
            # устаревший код категории Авито молча меняет на родительский раздел — чужие объявления в базу не берём
            raise FetchError(f"Авито перенаправил на {self._page.url[:120]} — ссылка источника устарела")
        return html

    def _fetch(self, url: str) -> str:
        try:
            if not self._page:
                self._start()
            self._count()
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
            # Сначала даём 30 с автоматической «проверке безопасности» (браузер проходит её сам),
            # потом, если окно видно, ждём человека с капчей.
            wait = 30.0 + (self.captcha_wait if not self.headless else 0.0)
            start, asked = time.time(), False
            while time.time() - start < wait:
                self._page.wait_for_timeout(2000)
                if self._snapshot():
                    self.last_status = 200
                    return self.last_html
                if not asked and time.time() - start >= 30 and not self.headless:
                    asked = True
                    self.captchas += 1
                    try:
                        self._page.bring_to_front()   # окно Chrome — на передний план, капчу проходит человек
                        if self.on_captcha:
                            self.on_captcha()
                    except Exception:
                        pass
                    print(f"⏸ Авито просит капчу. Пройдите её в открытом окне Chrome (жду до {self.captcha_wait:.0f} с)…",
                          flush=True)
            raise BlockedError(f"HTTP {self.last_status}: Авито блокирует запросы (IP/капча)")
        return self.last_html

    def check_listing(self, url: str) -> dict:
        """Открывает карточку: жива ли она. Возвращает state: active | inactive | unknown, описание, состояние."""
        try:
            if not self._page:
                self._start()
            self._count()
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(2500)
            html = self._page.content()
        except Exception:
            return {"state": "unknown"}
        return parse_listing_page(resp.status if resp else 0, html)

    def scan(self, url: str, max_pages: int | None = None) -> tuple[list[RawListing], bool]:
        """Возвращает (объявления, полный_ли_обход). Полный — дошли до последней страницы."""
        seen: dict[str, RawListing] = {}
        complete = False
        prev_ids: set[str] = set()
        max_pages = max_pages or self.max_pages
        for page in range(1, max_pages + 1):
            items = parse_search_html(self.fetch(with_page(url, page)))
            ids = {i.id for i in items}
            if not items or ids <= prev_ids:   # пусто или Авито отдаёт ту же последнюю страницу
                complete = True
                break
            for i in items:
                seen[i.id] = i
            prev_ids = ids
            if page < max_pages:
                self._sleep(random.uniform(self.delay, self.delay_max if self.delay_max > self.delay else self.delay * 2))
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
