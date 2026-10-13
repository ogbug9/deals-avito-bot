"""Источники сбора: категория + регион + фильтры Авито → ссылка поиска.

Список лежит в sources.json (правится из интерфейса). Пока файла нет — берутся DEFAULT_SOURCES.
Адреса категорий сняты с живого Авито 13.10.2026; хвост «-ASg…» — код категории, без него
Авито уводит на родительский раздел.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlencode

BASE = "https://www.avito.ru"
_K = "tovary_dlya_kompyutera/komplektuyuschie"

# ключ → (название, путь без региона)
CATEGORIES: dict[str, tuple[str, str]] = {
    "videokarty": ("Видеокарты", f"{_K}/videokarty-ASgBAgICAkTGB~pm7gmmZw"),
    "pc": ("Системные блоки", "nastolnye_kompyutery/sistemnye_bloki-ASgBAgICAUS02xKMqY0D"),
    "protsessory": ("Процессоры", f"{_K}/protsessory-ASgBAgICAkTGB~pm7gniZw"),
    "materinskie_platy": ("Материнские платы", f"{_K}/materinskie_platy-ASgBAgICAkTGB~pm7gnOZw"),
    "operativnaya_pamyat": ("Оперативная память", f"{_K}/operativnaya_pamyat-ASgBAgICAkTGB~pm7gnYZw"),
    "bloki_pitaniya": ("Блоки питания", f"{_K}/bloki_pitaniya-ASgBAgICAkTGB~pm7gmcZw"),
    "zhestkie_diski": ("Жёсткие диски и SSD", f"{_K}/zhestkie_diski-ASgBAgICAkTGB~pm7gmwZw"),
    "sistemy_ohlazhdeniya": ("Системы охлаждения", f"{_K}/sistemy_ohlazhdeniya-ASgBAgICAkTGB~pm7gn2Zw"),
    "korpusa": ("Корпуса", f"{_K}/korpusa-ASgBAgICAkTGB~pm7gnEZw"),
    "komplektuyuschie": ("Все комплектующие", "tovary_dlya_kompyutera/komplektuyuschie-ASgBAgICAUTGB~pm"),
}

REGIONS: dict[str, str] = {
    "moskva": "Москва",
    "moskva_i_mo": "Москва и область",
    "moskovskaya_oblast": "Московская область",
    "sankt-peterburg": "Санкт-Петербург",
    "all": "Вся Россия",
}


# Город продавца Авито пишет в адресе объявления: /krasnodar/tovary_dlya_kompyutera/… (в общероссийской
# выдаче другого источника города нет). Названия — для частых городов, остальные показываются как в адресе.
CITY_NAMES = {
    "moskva": "Москва", "moskva_zelenograd": "Зеленоград", "sankt-peterburg": "Санкт-Петербург",
    "novosibirsk": "Новосибирск", "ekaterinburg": "Екатеринбург", "kazan": "Казань", "nizhniy_novgorod": "Нижний Новгород",
    "chelyabinsk": "Челябинск", "samara": "Самара", "omsk": "Омск", "rostov-na-donu": "Ростов-на-Дону", "ufa": "Уфа",
    "krasnoyarsk": "Красноярск", "voronezh": "Воронеж", "perm": "Пермь", "volgograd": "Волгоград", "krasnodar": "Краснодар",
    "saratov": "Саратов", "tyumen": "Тюмень", "tolyatti": "Тольятти", "izhevsk": "Ижевск", "barnaul": "Барнаул",
    "irkutsk": "Иркутск", "ulyanovsk": "Ульяновск", "habarovsk": "Хабаровск", "yaroslavl": "Ярославль",
    "vladivostok": "Владивосток", "mahachkala": "Махачкала", "tomsk": "Томск", "orenburg": "Оренбург", "kemerovo": "Кемерово",
    "ryazan": "Рязань", "tula": "Тула", "kaliningrad": "Калининград", "sochi": "Сочи", "balashiha": "Балашиха",
    "himki": "Химки", "podolsk": "Подольск", "mytischi": "Мытищи", "lyubertsy": "Люберцы", "korolev": "Королёв",
}


def city_from_url(url: str) -> tuple[str, str]:
    """(код города из адреса объявления, название). ('', '') — если адрес не авитовский."""
    parts = (url or "").split("avito.ru/", 1)
    slug = parts[1].split("/", 1)[0] if len(parts) == 2 and "/" in parts[1] else ""
    return slug, CITY_NAMES.get(slug, slug.replace("_", " ").replace("-", " ").title() if slug else "")


@dataclass
class Source:
    name: str
    category: str = "videokarty"
    region: str = "moskva"
    delivery_only: bool = False      # только с Авито Доставкой (для «Вся Россия» — иначе в Москву не привезти)
    pmin: int = 0
    pmax: int = 0
    query: str = ""
    max_pages: int = 3
    enabled: bool = True

    @property
    def url(self) -> str:
        path = CATEGORIES[self.category][1]
        q: dict[str, str] = {}
        if self.delivery_only:
            q["d"] = "1"
        if self.pmin:
            q["pmin"] = str(int(self.pmin))
        if self.pmax:
            q["pmax"] = str(int(self.pmax))
        if self.query.strip():
            q["q"] = self.query.strip()
        q["s"] = "104"   # сортировка по дате
        return f"{BASE}/{self.region}/{path}?{urlencode(q)}"

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in d.items() if k in known})
        if s.category not in CATEGORIES:
            raise ValueError(f"неизвестная категория: {s.category}")
        if not s.region or not all(c.isalnum() or c in "-_" for c in s.region):
            raise ValueError(f"недопустимый регион: {s.region!r}")
        s.name = str(s.name).strip() or f"{CATEGORIES[s.category][0]} — {REGIONS.get(s.region, s.region)}"
        s.pmin, s.pmax = max(0, int(s.pmin or 0)), max(0, int(s.pmax or 0))
        s.max_pages = min(20, max(1, int(s.max_pages or 1)))
        s.delivery_only, s.enabled = bool(s.delivery_only), bool(s.enabled)
        return s


DEFAULT_SOURCES = [
    Source("Видеокарты — Москва", "videokarty", "moskva", max_pages=3),
    Source("Видеокарты — Россия, с доставкой", "videokarty", "all", delivery_only=True, max_pages=3),
    Source("Системные блоки — Москва", "pc", "moskva", max_pages=2),
    Source("Процессоры — Москва", "protsessory", "moskva", max_pages=2),
    Source("Материнские платы — Москва", "materinskie_platy", "moskva", max_pages=2),
    Source("Оперативная память — Москва", "operativnaya_pamyat", "moskva", max_pages=2),
    Source("Блоки питания — Москва", "bloki_pitaniya", "moskva", max_pages=1),
    Source("Жёсткие диски и SSD — Москва", "zhestkie_diski", "moskva", max_pages=1),
    Source("Системы охлаждения — Москва", "sistemy_ohlazhdeniya", "moskva", max_pages=1),
    Source("Корпуса — Москва", "korpusa", "moskva", max_pages=1),
]


def load(path: str = "sources.json") -> list[Source]:
    p = Path(path)
    if not p.exists():
        return [Source(**asdict(s)) for s in DEFAULT_SOURCES]
    return [Source.from_dict(d) for d in json.loads(p.read_text(encoding="utf-8"))]


def save(sources: list[Source], path: str = "sources.json") -> None:
    Path(path).write_text(json.dumps([asdict(s) for s in sources], ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
