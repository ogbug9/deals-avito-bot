"""Нормализаторы комплектующих (кроме видеокарт) и разбор целого ПК на детали.

Позиция (key) — то, по чему считается медиана: «RAM DDR4 16GB», «CPU Ryzen 5 5600X», «PSU 650W Gold»,
«SSD 512GB NVMe». Если позицию определить нельзя, возвращается None: цену такой детали не придумываем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import normalizer


@dataclass
class Part:
    kind: str                 # ram | cpu | psu | ssd | hdd | mb | gpu
    key: str                  # позиция для медианы
    qty: float = 0.0          # ГБ для памяти и дисков, Вт для БП — для удельной цены
    unit: str = ""            # «₽/ГБ», «₽/Вт»
    brand: str = ""
    detail: dict = field(default_factory=dict)
    fine_key: str = ""        # точная позиция (с брендом/частотой); используется, когда в ней хватает объявлений


def _low(*texts: str) -> str:
    return " ".join(t or "" for t in texts).lower().replace("ё", "е").replace("\xa0", " ")


def _brand(text: str, brands: tuple[str, ...]) -> str:
    for b in brands:
        if re.search(rf"(?<![a-zа-я]){re.escape(b)}(?![a-zа-я])", text):
            return b
    return ""


# ---------------------------------------------------------------- ОЗУ
_RAM_BRANDS = ("kingston", "hyperx", "fury", "corsair", "crucial", "ballistix", "samsung", "hynix", "micron", "adata",
               "xpg", "g.skill", "gskill", "patriot", "viper", "team", "kingbank", "netac", "apacer", "goodram",
               "transcend", "amd", "geil", "kingspec", "digma", "exegate", "foxline", "qumo", "silicon power")
_DDR_RE = re.compile(r"(?<![a-zа-я])(?:ddr|ддр)[\s\-]?([2345])(l)?")   # не GDDR5 — это видеопамять
_KIT_RE = re.compile(r"(\d)\s*(?:x|х|\*|шт\.?\s*по|планк\w*\s*по|модул\w*\s*по)\s*(\d{1,3})\s*(?:gb|гб|g\b|г\b)?")
_KIT_REV_RE = re.compile(r"(\d{1,3})\s*(?:gb|гб)\s*(?:x|х|\*)\s*(\d)(?!\d)")
_GB_RE = re.compile(r"(?<![0-9.,])(\d{1,3})\s*(?:gb|гб|g(?![a-zа-я])|гиг)")
_FREQ_RE = re.compile(r"(?<!\d)(1333|1600|1866|2133|2400|2666|2933|3000|3200|3466|3600|3733|4000|4400|4800|5200|5600|6000|6200|6400|6600|6800|7200|7400|7600|7800|8000|8200|8400)(?!\d)")
_SODIMM_RE = re.compile(r"so-?dimm|sodimm|для ноутбук|ноутбучн|для ноута")
_ECC_RE = re.compile(r"\becc\b|\breg\b|registered|rdimm|lrdimm|серверн")
RAM_SIZES = {1, 2, 4, 8, 16, 32, 48, 64, 96, 128}


def _ram_tier(ddr: int, freq: int | None) -> str:
    """Скоростная группа: быстрые комплекты заметно дороже. Частота не указана — считаем базовой."""
    if ddr == 4 and freq and freq >= 3000:
        return " 3000+"
    if ddr == 5 and freq and freq >= 6600:
        return " 6600+"
    if ddr == 5 and freq and freq >= 5600:
        return " 5600+"
    return ""


def parse_ram(title: str, description: str = "") -> Part | None:
    t, full = _low(title), _low(title, description)
    m = _DDR_RE.search(t) or _DDR_RE.search(full)
    if not m:
        return None                      # без типа памяти цена за ГБ ни о чём не говорит
    ddr = int(m.group(1))
    sticks, per = 1, 0
    k = _KIT_RE.search(t) or _KIT_REV_RE.search(t)
    if k:
        a, b = int(k.group(1)), int(k.group(2))
        sticks, per = (a, b) if k.re is _KIT_RE else (b, a)
    total = sticks * per
    if total not in RAM_SIZES or per not in RAM_SIZES:
        sizes = [int(x) for x in _GB_RE.findall(t)] or [int(x) for x in _GB_RE.findall(full)]
        sizes = [s for s in sizes if s in RAM_SIZES]
        if not sizes:
            return None
        total, sticks, per = max(sizes), 1, max(sizes)
        k2 = _KIT_RE.search(full) or _KIT_REV_RE.search(full)
        if k2:                           # «16 гб (2x8)» — объём из заголовка, число планок из описания
            a, b = int(k2.group(1)), int(k2.group(2))
            n, each = (a, b) if k2.re is _KIT_RE else (b, a)
            if n * each == total:
                sticks, per = n, each
    form = "SODIMM" if _SODIMM_RE.search(full) else "DIMM"
    f = _FREQ_RE.search(t) or _FREQ_RE.search(full)
    freq = int(f.group(1)) if f else None
    ecc = bool(_ECC_RE.search(full))
    key = f"RAM DDR{ddr} {total}GB" + _ram_tier(ddr, freq) + (" ECC" if ecc else "") + (" SODIMM" if form == "SODIMM" else "")
    brand = _brand(full, _RAM_BRANDS)
    fine = f"RAM DDR{ddr} {total}GB {freq} {brand.title()}" + (" ECC" if ecc else "") + \
           (" SODIMM" if form == "SODIMM" else "") if brand and freq else ""
    return Part("ram", key, qty=float(total), unit="₽/ГБ", brand=brand, fine_key=fine,
                detail={"ddr": ddr, "gb": total, "sticks": sticks, "kit": sticks > 1, "freq": freq, "form": form,
                        "ecc": ecc})


# ---------------------------------------------------------------- процессоры
_CPU_PATTERNS = [
    # Core Ultra 5 245K
    (re.compile(r"(?:core\s*)?ultra\s*([3579])\s*[\- ]?(\d{3})\s?([a-z]{0,2})(?![a-z0-9])"),
     lambda m: f"Core Ultra {m.group(1)} {m.group(2)}{m.group(3).upper()}"),
    # i5-12400F, i7 9700k, i3 10100
    (re.compile(r"(?<![a-z0-9])i([3579])[\s\-]*(\d{4,5})\s?([a-z]{0,2})(?![a-z0-9])"),
     lambda m: f"i{m.group(1)}-{m.group(2)}{m.group(3).upper()}"),
    # Ryzen 5 5600X, Ryzen 7 7800X3D, R5 3600
    (re.compile(r"(?:ryzen|райзен|(?<![a-z0-9])r)\s*([3579])\s*(?:pro\s*)?(\d{4})\s?(x3d|xt|x|g|ge|f|u|h)?(?![a-z0-9])"),
     lambda m: f"Ryzen {m.group(1)} {m.group(2)}{(m.group(3) or '').upper()}"),
    # Ryzen 7500F без цифры линейки
    (re.compile(r"(?:ryzen|райзен)\s*(\d{4})\s?(x3d|xt|x|g|ge|f)?(?![a-z0-9])"),
     lambda m: f"Ryzen {m.group(1)}{(m.group(2) or '').upper()}"),
    # Xeon E5-2670 v3, E3 1230v2
    (re.compile(r"(?:xeon\s*)?(?<![a-z0-9])e([357])[\s\-]*(\d{4})\s?(?:v\s?(\d))?(?![a-z0-9])"),
     lambda m: f"Xeon E{m.group(1)}-{m.group(2)}" + (f" v{m.group(3)}" if m.group(3) else "")),
    # Pentium / Celeron G5400, Athlon 3000G / 200GE
    (re.compile(r"(?:pentium|celeron)\s*(?:gold\s*)?(g\d{4}[a-z]?)(?![a-z0-9])"), lambda m: f"Pentium/Celeron {m.group(1).upper()}"),
    (re.compile(r"athlon\s*(?:x4\s*)?(\d{3,4}(?:ge|g)?)(?![a-z0-9])"), lambda m: f"Athlon {m.group(1).upper()}"),
    (re.compile(r"(?<![a-z0-9])fx[\s\-]*(\d{4})(?![a-z0-9])"), lambda m: f"FX-{m.group(1)}"),
]
_CPU_KIT_RE = re.compile(r"комплект|связк|набор|\+\s*(?:мат|плат|озу|кулер|памят)|материнск|с платой|с матерью")


def find_cpus(text: str) -> list[str]:
    found: list[str] = []
    for rx, fmt in _CPU_PATTERNS:
        for m in rx.finditer(text):
            name = fmt(m)
            if name not in found and not any(name in f or f in name for f in found):
                found.append(name)
    return found


def parse_cpu(title: str, description: str = "") -> Part | None:
    t = _low(title)
    if _CPU_KIT_RE.search(t) or "+" in t or {c for c in _CHIPSET_RE.findall(t) if c in _CHIPSETS}:
        return None                      # комплект «проц + плата» с одиночным процессором не сравнить
    cpus = find_cpus(t) or find_cpus(_low(description))
    if len(cpus) != 1:
        return None
    return Part("cpu", f"CPU {cpus[0]}", brand="intel" if cpus[0][0] in "iCXP" else "amd",
                detail={"tray": bool(re.search(r"\boem\b|tray|трей", _low(title, description)))})


# ---------------------------------------------------------------- блоки питания
_PSU_BRANDS = ("be quiet", "bequiet", "corsair", "seasonic", "chieftec", "deepcool", "cooler master", "coolermaster",
               "thermaltake", "aerocool", "fsp", "zalman", "cougar", "msi", "asus", "gigabyte", "1stplayer", "xpg",
               "montech", "exegate", "powerman", "hiper", "ginzzu", "super flower", "evga", "nzxt", "formula", "ocypus",
               "accord", "crown", "gamemax", "pccooler", "silverstone", "enermax", "antec", "inwin", "delta", "hp", "dell", "lenovo")
_PSU_TIER_A = {"be quiet", "bequiet", "corsair", "seasonic", "chieftec", "deepcool", "cooler master", "coolermaster",
               "thermaltake", "fsp", "zalman", "cougar", "msi", "asus", "gigabyte", "1stplayer", "xpg", "montech",
               "super flower", "evga", "nzxt", "silverstone", "enermax", "antec", "pccooler"}
_WATT_RE = re.compile(r"(?<![0-9])(\d{3,4})\s*(?:w(?![a-z])|вт|ватт|watt)")
_WATT_MODEL_RE = re.compile(r"(?<![0-9a-z])(?:[a-z]{1,4}[\- ]?)?(\d{3,4})(?:w|вт)?(?![0-9])")
_CERT_RE = re.compile(r"(?:80\s*(?:\+|plus)\s*)?(titanium|platinum|gold|silver|bronze|white|standard|титан|платин|золот|серебр|бронз)")
_CERT_RU = {"титан": "Titanium", "платин": "Platinum", "золот": "Gold", "серебр": "Silver", "бронз": "Bronze"}
PSU_WATTS = (300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 1000, 1200, 1300, 1500, 1600)


def parse_psu(title: str, description: str = "") -> Part | None:
    t, full = _low(title), _low(title, description)
    if re.search(r"ноутбук|адаптер|зарядк|сервер|кабел|провод|удлинит", t):
        return None
    m = _WATT_RE.search(t) or _WATT_RE.search(full)
    watts = int(m.group(1)) if m else None
    if watts is None:                    # «Deepcool PF700», «Chieftec GPS-650A8» — мощность в названии модели
        cand = [int(x) for x in _WATT_MODEL_RE.findall(t) if int(x) in PSU_WATTS]
        watts = cand[0] if len(cand) == 1 else None
    if watts is None or not 250 <= watts <= 2000:
        return None
    c = _CERT_RE.search(t) or (_CERT_RE.search(full) if "80" in full else None)
    cert = ""
    if c:
        cert = _CERT_RU.get(c.group(1), c.group(1).capitalize())
        cert = "" if cert in ("White", "Standard") else cert
    watts = min(PSU_WATTS, key=lambda w: abs(w - watts))
    brand = _brand(full, _PSU_BRANDS)
    tier = " (бренд A)" if brand in _PSU_TIER_A else ""      # Seasonic за 650 Вт и безымянный — разные рынки
    fine = f"PSU {watts}W" + (f" {cert}" if cert else "") + f" {brand.title()}" if brand else ""
    return Part("psu", f"PSU {watts}W" + (f" {cert}" if cert else "") + tier, qty=float(watts), unit="₽/Вт",
                brand=brand, fine_key=fine, detail={"watts": watts, "cert": cert or None})


# ---------------------------------------------------------------- диски
_DISK_BRANDS = ("samsung", "kingston", "wd", "western digital", "seagate", "toshiba", "crucial", "adata", "xpg", "sandisk",
                "intel", "hitachi", "hgst", "netac", "apacer", "transcend", "patriot", "msi", "gigabyte", "kioxia", "digma",
                "kingspec", "silicon power", "goodram", "micron", "sk hynix", "hikvision", "lexar", "team")
_CAP_RE = re.compile(r"(?<![0-9.,])(\d{1,4}(?:[.,]\d)?)\s*(tb|тб|терабайт|gb|гб|g(?![a-zа-я])|гиг)")
_DISK_BUCKETS = (120, 128, 240, 250, 256, 320, 480, 500, 512, 750, 960, 1000, 1024, 2000, 3000, 4000, 6000, 8000,
                 10000, 12000, 14000, 16000, 18000, 20000)
_DISK_CANON = {120: 128, 240: 256, 250: 256, 480: 512, 500: 512, 960: 1000, 1024: 1000}


def _capacity(text: str) -> int | None:
    best = None
    for num, unit in _CAP_RE.findall(text):
        gb = float(num.replace(",", ".")) * (1000 if unit in ("tb", "тб", "терабайт") else 1)
        if gb < 100:
            continue                     # 8 ГБ, 16 ГБ — это не про диск
        bucket = min(_DISK_BUCKETS, key=lambda b: abs(b - gb))
        if abs(bucket - gb) <= gb * 0.08:
            best = max(best or 0, _DISK_CANON.get(bucket, bucket))
    return best


def parse_disk(title: str, description: str = "") -> Part | None:
    t, full = _low(title), _low(title, description)
    if re.search(r"корзин|салазк|переходник|кабел|бокс|карман|док-станц|контроллер|внешн", t):
        return None
    is_ssd = bool(re.search(r"ssd|ссд|nvme|твердотел|m\.?2", t))
    is_hdd = bool(re.search(r"hdd|жестк|винчестер|sas\b|rpm|об/мин", t))
    if is_ssd == is_hdd:                 # оба или ни одного: уточняем по описанию
        is_ssd = bool(re.search(r"ssd|ссд|nvme|твердотел", full)) and not is_hdd
        is_hdd = not is_ssd and bool(re.search(r"hdd|жестк|винчестер|rpm", full))
    if not (is_ssd or is_hdd):
        return None
    cap = _capacity(t) or _capacity(full)
    if not cap:
        return None
    size = f"{cap // 1000}TB" if cap >= 1000 else f"{cap}GB"
    if is_ssd:
        iface = "NVMe" if re.search(r"nvme|m\.?2|pci-?e", full) and not re.search(r"m\.?2\s*sata", full) else "SATA"
        key = f"SSD {size} {iface}"
    else:
        form = '2.5"' if re.search(r"2[.,]5|ноутбук", full) else '3.5"'
        key = f"HDD {size} {form}"
    brand = _brand(full, _DISK_BRANDS)
    return Part("ssd" if is_ssd else "hdd", key, qty=float(cap), unit="₽/ГБ", brand=brand,
                fine_key=f"{key} {brand.title()}" if brand else "", detail={"gb": cap})


# ---------------------------------------------------------------- материнские платы (для разбора ПК)
_CHIPSET_FULL_RE = re.compile(r"(?<![a-z0-9])([abhxzq]\d{2,3})(m|i|e)?(?![0-9])")
_CHIPSET_RE = re.compile(r"(?<![a-z0-9])([abhxzq]\d{2,3})(?:m|i|e)?(?![0-9])")
_SOCKETS = {
    "AM4": ("a320", "a520", "b350", "b450", "b550", "x370", "x470", "x570"),
    "AM5": ("a620", "b650", "b850", "x670", "x870"),
    "LGA1155": ("h61", "b75", "z77"), "LGA1150": ("h81", "b85", "h97", "z87", "z97"),
    "LGA1151": ("h110", "b150", "h170", "z170", "b250", "h270", "z270", "h310", "b360", "b365", "h370", "z370", "z390"),
    "LGA1200": ("h410", "b460", "h470", "z490", "h510", "b560", "h570", "z590"),
    "LGA1700": ("h610", "b660", "h670", "z690", "b760", "h770", "z790"),
    "LGA1851": ("h810", "b860", "z890"),
    "LGA2011": ("x79",), "LGA2011-3": ("x99",), "LGA2066": ("x299",),
}
SOCKET_BY_CHIPSET = {c: sock for sock, chips in _SOCKETS.items() for c in chips}
_MB_BRANDS = ("asus", "msi", "gigabyte", "asrock", "biostar", "maxsun", "colorful", "huananzhi", "machinist", "esonic",
              "afox", "supermicro", "intel")


def mb_position(text: str) -> tuple[str, str] | None:
    """(ключ «MB сокет чипсет форм-фактор», чипсет) или None, если чипсет не один."""
    hits = {(c, suf) for c, suf in _CHIPSET_FULL_RE.findall(text) if c in _CHIPSETS}
    chips = {c for c, _ in hits}
    if len(chips) != 1:
        return None
    chip = next(iter(chips))
    suffixes = {suf for _, suf in hits}
    if re.search(r"mini[\s\-]?itx|\bitx\b|-itx", text) or "i" in suffixes:
        form = "ITX"
    elif re.search(r"micro[\s\-]?atx|m-?atx|\bmatx\b", text) or "m" in suffixes:
        form = "mATX"
    else:
        form = "ATX"
    return f"MB {SOCKET_BY_CHIPSET.get(chip, '?')} {chip.upper()} {form}", chip
_CHIPSETS = {"a320", "a520", "a620", "b350", "b450", "b550", "b650", "b850", "x370", "x470", "x570", "x670", "x870",
             "h61", "h81", "h97", "h110", "h170", "h270", "h310", "h370", "h410", "h470", "h510", "h570", "h610", "h670",
             "h770", "h810", "b75", "b85", "b150", "b250", "b360", "b365", "b460", "b560", "b660", "b760", "b860",
             "z77", "z87", "z97", "z170", "z270", "z370", "z390", "z490", "z590", "z690", "z790", "z890", "x79", "x99", "x299"}


# Линейка платы: у одного бренда и чипсета ROG Hero и TUF отличаются по цене вдвое — в одну точную позицию их не мешаем.
_MB_LINES = (
    ("топ", re.compile(r"maximus|crosshair|(?<![a-z])hero(?![a-z])|extreme|xtreme|formula|(?<![a-z])apex(?![a-z])|aorus\s*master|"
                       r"taichi|godlike|(?<![a-z])meg(?![a-z])|(?<![a-z])ace(?![a-z])|unify|(?<![a-z])aqua(?![a-z])")),
    ("средняя", re.compile(r"strix|(?<![a-z])rog(?![a-z])|aorus|steel\s*legend|lightning|(?<![a-z])mpg(?![a-z])|carbon|"
                           r"(?<![a-z])edge(?![a-z])|tomahawk|proart|(?<![a-z])nova(?![a-z])|riptide")),
    ("базовая", re.compile(r"prime|(?<![a-z])tuf(?![a-z])|(?<![a-z])pro(?![a-z])|gaming\s*x|eagle|ds3h|(?<![a-z])ud(?![a-z])|"
                           r"hdv|phantom|gaming\s*plus|(?<![a-z])plus(?![a-z])|(?<![a-z])elite(?![a-z])|(?<![a-z])h[dv]s(?![a-z])")),
)


def mb_line(text: str) -> str:
    """«топ» / «средняя» / «базовая» по названию серии, '' — не понять."""
    for name, rx in _MB_LINES:
        if rx.search(text):
            return name
    return ""


def mb_line_near(text: str, chipset: str) -> str:
    """Линейка рядом с чипсетом: в описании ПК «Windows 11 Pro» или «Samsung 980 Pro» к плате не относятся."""
    i = text.find(chipset.lower())
    return mb_line(text[max(0, i - 30):i + 45]) if i >= 0 else ""


def mb_key(pos_key: str, line: str) -> str:
    """Позиция платы: сокет + чипсет + форм-фактор и, если понятна, линейка — базовые сравниваются с базовыми."""
    return f"{pos_key} ({line})" if line else pos_key


def parse_mb(title: str, description: str = "") -> Part | None:
    t = _low(title)
    if re.search(r"комплект|связк|набор|\+\s*(?:проц|озу|памят|cpu)|с процессор", t):
        return None
    pos = mb_position(t) or mb_position(_low(title, description))
    if not pos:
        return None
    brand = _brand(_low(title, description), _MB_BRANDS)
    line = mb_line(t) or mb_line_near(_low(description), pos[1])
    fine = f"{pos[0]} {brand.title()}" + (f" ({line})" if line else "") if brand else ""
    return Part("mb", mb_key(pos[0], line), brand=brand, fine_key=fine, detail={"chipset": pos[1], "line": line or None})


PARSERS = {
    "operativnaya_pamyat": parse_ram,
    "protsessory": parse_cpu,
    "bloki_pitaniya": parse_psu,
    "zhestkie_diski": parse_disk,
    "materinskie_platy": parse_mb,
}


def parse_part(category: str, title: str, description: str = "") -> Part | None:
    fn = PARSERS.get(category)
    return fn(title, description) if fn else None


# ---------------------------------------------------------------- целый ПК → детали
_PC_REJECT = [
    (re.compile(r"ноутбук|laptop|macbook|нетбук"), "laptop"),
    (re.compile(r"моноблок|imac|all-in-one"), "monoblock"),
    (re.compile(r"mac mini|mac studio|неттоп|мини[\s\-]?пк|mini[\s\-]?pc|nuc\b|тонкий клиент|сервер"), "not_desktop"),
]
_ABSENT = {   # «без видеокарты» → детали нет, в сумму не идёт
    "gpu": re.compile(r"без\s+(?:дискретн\w+\s+)?видеокарт|без\s+видюх|видеокарт\w*\s+(?:нет|отсутству)|встроенн\w+\s+(?:видео|график)"),
    "psu": re.compile(r"без\s+(?:блока\s+питания|бп)\b|бп\s+(?:нет|отсутству)"),
    "ram": re.compile(r"без\s+(?:оперативн|озу|памяти)"),
    "disk": re.compile(r"без\s+(?:жестк\w+\s+диск|ssd|hdd|диск|накопител)|ssd[\s\-]*отсутс|диск\w*\s+(?:нет|отсутс)"),
    "cpu": re.compile(r"без\s+процессор"),
}


@dataclass
class Bundle:
    parts: list[Part] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)     # что должно быть в ПК, но не распознано
    absent: list[str] = field(default_factory=list)      # продавец прямо пишет, что этого нет
    reject: str = ""


def parse_pc(title: str, description: str = "") -> Bundle:
    t, full = _low(title), _low(title, description)
    b = Bundle()
    for rx, reason in _PC_REJECT:
        if rx.search(t):
            b.reject = reason
            return b
    b.absent = [k for k, rx in _ABSENT.items() if rx.search(full)]

    if "gpu" not in b.absent:
        models = normalizer._find_models(full, full + " видеокарта")
        if len(models) == 1:
            model = next(iter(models))
            key, vram = model, normalizer._vram_near(full, model)
            if model in normalizer.VRAM_VARIANTS:
                allowed = normalizer.VRAM_VARIANTS[model]
                vram = vram if vram in allowed else (next(iter(allowed)) if len(allowed) == 1 else None)
                key = f"{model} {vram}GB" if vram else ""
            if key:
                b.parts.append(Part("gpu", key))
            else:
                b.unknown.append("gpu")
        else:
            b.unknown.append("gpu")      # ни одной или сразу несколько («RTX 5080 / 5070 на выбор»)
    if "cpu" not in b.absent:
        cpus = find_cpus(full)
        b.parts.append(Part("cpu", f"CPU {cpus[0]}")) if len(cpus) == 1 else b.unknown.append("cpu")
    if "ram" not in b.absent:
        ram = _pc_ram(full)
        b.parts.append(ram) if ram else b.unknown.append("ram")
    mb = mb_position(full)
    b.parts.append(Part("mb", mb_key(mb[0], mb_line_near(full, mb[1])))) if mb else b.unknown.append("mb")
    if "psu" not in b.absent:
        psu = _pc_psu(full)
        b.parts.append(psu) if psu else b.unknown.append("psu")
    if "disk" not in b.absent:
        disks = _pc_disks(full)
        b.parts.extend(disks) if disks else b.unknown.append("disk")
    b.unknown.append("case")             # корпус по тексту не оценить
    return b


_DDR_BY_SOCKET = {"AM4": 4, "AM5": 5, "LGA1151": 4, "LGA1200": 4, "LGA1155": 3, "LGA1150": 3, "LGA1156": 3, "AM3": 3, "AM3+": 3}


def platform_ddr(full: str) -> int | None:
    """Тип памяти по платформе, когда продавец его не написал: Ryzen 5 3600 — это DDR4, i5-3470 — DDR3.
    Там, где платформа держит оба типа (LGA1700: 12–14-е поколение Intel), не гадаем."""
    mb = mb_position(full)
    if mb:
        ddr = _DDR_BY_SOCKET.get(mb[0].split()[1])
        if ddr:
            return ddr
    cpus = find_cpus(full)
    if len(cpus) != 1:
        return None
    m = re.fullmatch(r"Ryzen (?:[3579] )?(\d)\d{3}\w*", cpus[0])
    if m:
        return 4 if m.group(1) in "12345" else 5 if m.group(1) in "789" else None
    m = re.fullmatch(r"i[3579]-(\d{4,5})\w*", cpus[0])
    if m:
        gen = int(m.group(1)[:2]) if len(m.group(1)) == 5 else int(m.group(1)[0])
        return 3 if gen in (2, 3, 4) else 4 if gen in (6, 7, 8, 9, 10, 11) else None
    return None


_PC_RAM_RES = [re.compile(x) for x in (
    r"(?<![a-zа-я])(?:озу|оперативн\w*(?:\s+памят\w*)?|памят\w*|ram)\W{0,12}(?:ddr\s?\d\w?\W{0,4})?(?:(\d)\s*[xх*]\s*)?(\d{1,3})\s*(?:gb|гб|g\b|гиг)",
    r"(?<![0-9])(?:(\d)\s*[xх*]\s*)?(\d{1,3})\s*(?:gb|гб|g\b|гиг)\w*\s*(?:ddr\s?\d\w?\s*)?(?:озу|оперативн|ram|памят)",
    r"ddr\s?\d\w?\W{0,4}(?:(\d)\s*[xх*]\s*)?(\d{1,3})\s*(?:gb|гб|g\b)",
)]


def _pc_ram(full: str) -> Part | None:
    total = 0
    for rx in _PC_RAM_RES:           # «16gbram/500gbnvme»: первое совпадение может оказаться объёмом диска
        for m in rx.finditer(full):
            size = int(m.group(2)) * int(m.group(1) or 1)
            if size in RAM_SIZES:
                total = size
                break
        if total:
            break
    if not total:
        return None
    ddr = _DDR_RE.search(full)
    gen = int(ddr.group(1)) if ddr else platform_ddr(full)
    if not gen:
        return None                      # объём без типа памяти не оценить
    return Part("ram", f"RAM DDR{gen} {total}GB", qty=float(total), unit="₽/ГБ",
                detail={} if ddr else {"inferred": "тип памяти не написан, взят по платформе"})


def _pc_psu(full: str) -> Part | None:
    m = re.search(r"(?:блок\w*\s+питани\w*|бп)\W{0,40}?(\d{3,4})\s*(?:w(?![a-z])|вт|ватт)", full) \
        or re.search(r"(?<![0-9])(\d{3,4})\s*(?:w(?![a-z])|вт|ватт)", full)
    if not m or not 250 <= int(m.group(1)) <= 2000:
        return None
    watts = min(PSU_WATTS, key=lambda w: abs(w - int(m.group(1))))
    tail = full[m.start():m.end() + 40]
    c = _CERT_RE.search(tail)
    cert = _CERT_RU.get(c.group(1), c.group(1).capitalize()) if c else ""
    cert = "" if cert in ("White", "Standard") else cert
    return Part("psu", f"PSU {watts}W" + (f" {cert}" if cert else ""), qty=float(watts), unit="₽/Вт")


def _pc_disks(full: str) -> list[Part]:
    out: list[Part] = []
    for kind, rx in (("ssd", r"ssd|ссд|nvme|m\.?2"), ("hdd", r"hdd|жестк\w+\s+диск\w*|винчестер")):
        m = re.search(rf"(?:{rx})\W{{0,12}}(?:[a-zа-я.]+\W{{0,3}}){{0,3}}?(\d{{1,4}})\s*(tb|тб|gb|гб)", full) \
            or re.search(rf"(?<![0-9.,])(\d{{1,4}})\s*(tb|тб|gb|гб)\W{{0,4}}(?:{rx})", full)
        if not m and kind == "ssd":      # «ssd 512» — объём без единиц, только типовые значения
            bare = re.search(r"(?:ssd|ссд|nvme)\W{0,3}(120|128|240|250|256|480|500|512|960|1000|1024)(?![0-9])", full)
            m = bare and re.match(r"(\d+)()", bare.group(1))
        if not m:
            continue
        cap = _capacity(f"{m.group(1)} {m.group(2) or 'gb'}")
        if not cap:
            continue
        size = f"{cap // 1000}TB" if cap >= 1000 else f"{cap}GB"
        if kind == "ssd":
            iface = "NVMe" if re.search(r"nvme|m\.?2", full) else "SATA"
            out.append(Part("ssd", f"SSD {size} {iface}", qty=float(cap), unit="₽/ГБ"))
        else:
            out.append(Part("hdd", f'HDD {size} 3.5"', qty=float(cap), unit="₽/ГБ"))
    return out
