"""Нормализация объявления: из заголовка/описания достаём модель видеокарты, объём памяти и риск-флаги."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- каталог моделей -------------------------------------------------------
NV_SUFFIXES = {
    "750": {"", "ti"}, "950": {""}, "960": {""}, "970": {""}, "980": {"", "ti"},
    "1050": {"", "ti"}, "1060": {""}, "1070": {"", "ti"}, "1080": {"", "ti"},
    "1630": {""}, "1650": {"", "super"}, "1660": {"", "super", "ti"},
    "2060": {"", "super"}, "2070": {"", "super"}, "2080": {"", "super", "ti"},
    "3050": {""}, "3060": {"", "ti"}, "3070": {"", "ti"}, "3080": {"", "ti"}, "3090": {"", "ti"},
    "4060": {"", "ti"}, "4070": {"", "super", "ti"}, "4080": {"", "super"}, "4090": {""},
    "5050": {""}, "5060": {"", "ti"}, "5070": {"", "ti"}, "5080": {""}, "5090": {""},
}
AMD_SUFFIXES = {
    "570": {""}, "580": {""}, "590": {""},
    "5500": {"xt"}, "5600": {"xt"}, "5700": {"", "xt"},
    "6500": {"xt"}, "6600": {"", "xt"}, "6650": {"xt"}, "6700": {"", "xt"}, "6750": {"xt"},
    "6800": {"", "xt"}, "6900": {"xt"}, "7600": {"", "xt"}, "7700": {"", "xt"},
    "7800": {"xt"}, "7900": {"xt", "xtx", "gre"},
}
# модели, где объём памяти меняет цену — оценивать без него нельзя
VRAM_VARIANTS = {
    "GTX 1060": {3, 6}, "GTX 1050 Ti": {4}, "RTX 2060": {6, 12}, "RTX 3060": {8, 12},
    "RTX 3050": {6, 8}, "RTX 4060 Ti": {8, 16}, "RTX 5060 Ti": {8, 16},
    "RX 570": {4, 8}, "RX 580": {4, 8}, "RX 5500 XT": {4, 8}, "RX 6600": {8}, "RX 7600": {8},
}
VALID_VRAM = {2, 3, 4, 6, 8, 10, 11, 12, 16, 20, 24, 32}
# модели с единственным объёмом памяти: другой объём в названии — ошибка продавца или подделка (перешитая старая карта)
FIXED_VRAM = {
    "GTX 1070": 8, "GTX 1070 Ti": 8, "GTX 1080": 8, "GTX 1080 Ti": 11, "GTX 1650 Super": 4,
    "GTX 1660": 6, "GTX 1660 Super": 6, "GTX 1660 Ti": 6, "RTX 2060 Super": 8, "RTX 2070": 8, "RTX 2070 Super": 8,
    "RTX 2080": 8, "RTX 2080 Super": 8, "RTX 2080 Ti": 11, "RTX 3060 Ti": 8, "RTX 3070": 8, "RTX 3070 Ti": 8,
    "RTX 3080 Ti": 12, "RTX 3090": 24, "RTX 3090 Ti": 24, "RTX 4060": 8, "RTX 4070": 12, "RTX 4070 Super": 12,
    "RTX 4080": 16, "RTX 4080 Super": 16, "RTX 4090": 24, "RTX 5070": 12, "RTX 5070 Ti": 16, "RTX 5080": 16, "RTX 5090": 32,
    "RX 5700": 8, "RX 5700 XT": 8, "RX 6600 XT": 8, "RX 6700 XT": 12, "RX 6800": 16, "RX 6800 XT": 16, "RX 6900 XT": 16,
    "RX 7800 XT": 16,
}

_T = r"[tт]"
_X = r"[xх]"
_I = r"[iи]"
_MODEL_RE = re.compile(
    r"(?<![a-zа-я0-9])"
    rf"(?P<pre>[rр]{_T}{_X}|[gг]{_T}{_X}|gt|[rр]{_X})?[\s\-]*"
    r"(?P<num>\d{3,4})"
    rf"(?P<suf>[\s\-]*(?:{_T}{_I}|super|супер|{_X}{_T}{_X}|{_X}{_T}|gre)|s(?![a-zа-я0-9]))?"
    r"(?![0-9])"
)
_CONTEXT_RE = re.compile(
    r"видеокарт|видюх|gpu|geforce|nvidia|radeon|amd|palit|msi|gigabyte|asus|zotac|inno3d|"
    r"gainward|sapphire|powercolor|kfa2|galax|afox|colorful|xfx|asrock|evga|pny|dexp"
)
_VRAM_RE = re.compile(r"(?<![0-9])(\d{1,2})\s*(?:gb|гб|g(?![a-zа-я])|г(?![а-я]))")

_PC_RE = re.compile(r"системн\w+ блок|игров\w+ пк|\bпк\b|компьютер|в сборе|сборк|ноутбук|laptop|моноблок")
_WANTED_RE = re.compile(r"\bкуплю\b|\bищу\b|\bскуплю\b|\bобмен\b|\bменяю\b|\bвыкуп\b")
_NOT_GPU_RE = re.compile(r"кулер|водоблок|переходник|кронштейн|подставк|райзер|термопрокладк|термопаст|"
                         r"охлаждени|радиатор|блок питания|материнск|процессор|оперативн")

from . import wordlists

_LISTS: dict = {}


def reload_words() -> None:
    """Сбросить кэш списков слов (после правки файлов в words/)."""
    _LISTS.clear()


def _words(kind: str):
    if kind not in _LISTS:
        _LISTS[kind] = {"stop": wordlists.stop_words, "risk": wordlists.risk_flags, "urgent": wordlists.urgent_words,
                        "junk": wordlists.junk_rules}[kind]()
    return _LISTS[kind]


# Продавцы пишут «без артефактов», «не майнилась» — это не признак брака. Такие обороты вырезаем
# перед поиском стоп-слов и флагов. Слова взяты из живой выдачи Авито (октябрь 2026).
_NEG_K = r"(?:артефакт|вылет|зависа|ремонт|майнинг|майнер|ферм|дефект|проблем|перегрев|сбо[ея]|отвал|нарекани|нюанс)\w*"
_NEGATIONS = [
    re.compile(rf"\b(?:без|нет|никаких|никакого|не было|ни)\s+(?:(?!{_NEG_K})[\w\-]+\s+){{0,4}}?{_NEG_K}"
               rf"(?:(?:\s*,\s*|\s+и\s+|\s+или\s+)(?:(?!{_NEG_K})[\w\-]+\s+){{0,2}}?{_NEG_K})*"),
    re.compile(rf"{_NEG_K}\s+(?:нет|отсутству\w+|не\s+(?:был\w*|участвовал\w*|использовал\w*|наблюда\w+|замечен\w*|обнаружен\w*))"),
    # рабочая вещь с оговоркой: «можно и на запчасти», «можно разобрать на детали», «хоть на донора»
    re.compile(r"(?:можно|хоть)\s+(?:и\s+|даже\s+)?(?:(?:разобрать\s+)?(?:на|под)\s+(?:запчаст\w*|детал\w*|разбор\w*)|на\s+донор\w*)"),
    re.compile(r"\bне\s+(?:майнил\w*|ремонтировал\w*|чинил\w*|прошивал\w*|перепрошивал\w*|гре[лею]т?\w*|перегрева\w*"
               r"|(?:был\w*\s+|использовал\w*\s+)?(?:в|для)\s+майнинг\w*|из[\s\-]под\s+майнинг\w*|с\s+фермы|после\s+ремонта)"),
]
# Латинские буквы-двойники внутри русских слов («бeз aртефактов») — частый приём против автопоиска.
_LOOKALIKE = str.maketrans("aeopcxykmtbh", "аеорсхукмтвн")
_WORD_RE = re.compile(r"[a-zа-я]+")


def _clean_for_flags(text: str) -> str:
    text = _WORD_RE.sub(lambda m: m.group().translate(_LOOKALIKE) if re.search("[а-я]", m.group()) else m.group(), text)
    for rx in _NEGATIONS:
        text = rx.sub(" ", text)
    return text


@dataclass
class Parsed:
    kind: str = "gpu"                 # gpu | pc | skip
    skip_reason: str = ""
    model: str = ""                   # "RTX 3060 Ti"
    vram: int | None = None
    key: str = ""                     # ключ для рынка: "RTX 3060 Ti" или "RTX 3060 12GB"
    flags: dict[str, float] = field(default_factory=dict)   # флаг -> вероятность проблемы
    info: list[str] = field(default_factory=list)
    model_source: str = ""            # title | description | fuzzy
    mobile: bool = False              # ноутбучная карта (3070m, Max-Q, MXM) — отдельная модель и рынок

    @property
    def ok(self) -> bool:
        return self.kind == "gpu" and bool(self.key)


def _canon(pre: str, num: str, suf: str) -> str | None:
    suf = re.sub(r"[\s\-]", "", suf or "")
    suf = suf.replace("ти", "ti").replace("супер", "super").replace("хт", "xt")
    suf = suf.replace("т", "t").replace("х", "x").replace("и", "i")
    if suf == "s":
        suf = "super"
    pre = (pre or "").replace("х", "x").replace("т", "t").replace("р", "r").replace("г", "g")
    if num in NV_SUFFIXES and (pre in ("", "rtx", "gtx", "gt") or not pre):
        if pre == "rx":
            return None
        if suf in NV_SUFFIXES[num]:
            n = int(num)
            brand = "GTX" if n < 2000 else "RTX"
            tail = {"": "", "ti": " Ti", "super": " Super"}[suf]
            return f"{brand} {num}{tail}"
        return None
    if num in AMD_SUFFIXES and pre in ("", "rx"):
        if suf in AMD_SUFFIXES[num]:
            return f"RX {num}" + (f" {suf.upper()}" if suf else "")
        return None
    return None


# У старых GeForce те же числа, что у Radeon RX: 7600 GS, 6600 GT, 7900 GTX, FX 5700. Без «rx» это не Radeon.
_OLD_NV_TAIL = re.compile(r"[\s\-]*(?:gs|gt|gtx|gts|gso|le|ultra)(?![a-zа-я0-9])")
_OLD_NV_HEAD = re.compile(r"(?:geforce|nvidia|fx)[\s\-]*$")
# Майнинговые карты без видеовыходов: «аналог 1060» в описании не делает их GTX 1060 — рынок и цена свои.
_MINING_RE = re.compile(r"(?<![a-zа-я0-9])[pр][\s\-]?(10[246])(?:[\s\-]*(100|090))?(?![0-9])")
# В названии прямо названа карта, которой нет в каталоге (gt 625, gtx 550, hd 7850): модель из описания не берём,
# там обычно сравнение («конкурент rtx 5090»), а не сама вещь.
_OTHER_GPU_RE = re.compile(r"(?<![a-zа-я0-9])(?:gt|gts|gtx|rtx|rx|hd|r[579]|geforce|quadro)[\s\-]?\d{3,4}")


def _mining_models(text: str) -> set[str]:
    return {f"P{m.group(1)}-{m.group(2) or '100'}" for m in _MINING_RE.finditer(text)}


def _find_models(text: str, ctx_text: str) -> set[str]:
    """Префикс (rtx/gtx/rx) принимаем сразу; голые цифры — только если рядом есть контекст видеокарты."""
    found: set[str] = set()
    has_ctx = bool(_CONTEXT_RE.search(ctx_text))
    for m in _MODEL_RE.finditer(text):
        pre = m.group("pre")
        if not pre and not has_ctx:
            continue
        if not pre and m.group("num") in AMD_SUFFIXES and (
                _OLD_NV_TAIL.match(text, m.end()) or _OLD_NV_HEAD.search(text[:m.start()])):
            continue
        name = _canon(pre or "", m.group("num"), m.group("suf") or "")
        if name:
            found.add(name)
    return found


def _vram(text: str) -> int | None:
    for m in _VRAM_RE.finditer(text):
        v = int(m.group(1))
        if v in VALID_VRAM:
            return v
    return None


# Опечатки и варианты написания: слово → как его понимать. Применяется, только если обычный разбор не нашёл модель.
TYPO_WORDS = {
    "rxt": "rtx", "rtz": "rtx", "trx": "rtx", "rtxx": "rtx", "ртикс": "rtx", "эртэикс": "rtx", "ртыкс": "rtx",
    "gxt": "gtx", "gtz": "gtx", "tgx": "gtx", "gtxx": "gtx", "гтикс": "gtx", "джитиикс": "gtx", "жтх": "gtx",
    "радеон": "radeon", "radion": "radeon", "raedon": "radeon", "радик": "radeon",
    "джифорс": "geforce", "жифорс": "geforce", "гефорс": "geforce", "geforse": "geforce", "gforce": "geforce",
    "нвидиа": "nvidia", "нвидия": "nvidia", "энвидиа": "nvidia", "nvidea": "nvidia", "invidia": "nvidia",
    "тай": "ti", "тиай": "ti", "титаниум": "ti", "суперка": "super", "supper": "super", "sup": "super",
    "видяха": "видеокарта", "видюха": "видеокарта", "видик": "видеокарта", "видеокрта": "видеокарта",
    "видиокарта": "видеокарта", "видеакарта": "видеокарта", "videokarta": "видеокарта",
}
_DIGIT_LOOKALIKE = str.maketrans("oоOОзЗlбБ", "000033166")
_NUMLIKE_RE = re.compile(r"(?<![a-zа-я0-9])[0-9oозlб]{3,4}(?![0-9])")
_SPLIT_NUM_RE = re.compile(r"((?:rtx|gtx|rx|gt)\s?)(\d{2})[\s\-](\d{2})(?!\d)")


def _fuzzy_text(text: str) -> str:
    text = _WORD_RE.sub(lambda m: TYPO_WORDS.get(m.group(), m.group()), text)
    text = _NUMLIKE_RE.sub(lambda m: m.group().translate(_DIGIT_LOOKALIKE) if sum(c.isdigit() for c in m.group()) >= 2
                           else m.group(), text)
    return _SPLIT_NUM_RE.sub(r"\1\2\3", text)   # «rtx 30 60» → «rtx 3060»


_MOBILE_ANY = re.compile(r"из ноутбука|от ноутбука|с ноутбука|ноутбучн|\bmxm\b|max-?q|\bmobile\b|мобильн\w+ (?:видео|верси)")
_MOBILE_TITLE = re.compile(r"laptop|для ноутбука")


def _is_mobile(t: str, full: str, model: str) -> bool:
    num = re.search(r"\d+", model).group()
    if re.search(rf"{num}\s?(?:ti\s?)?m(?![a-zа-я0-9])", full):   # 3070m, 3060 m — но не «3070 msi»
        return True
    return bool(_MOBILE_ANY.search(full) or _MOBILE_TITLE.search(t))


def condition(title: str, description: str = "") -> tuple[bool, bool]:
    """(брак, «куплю») для объявления любой категории — по тем же стоп-словам, что и для видеокарт."""
    t = (title or "").lower().replace("ё", "е")
    chk = _clean_for_flags(f"{t} {(description or '').lower().replace('ё', 'е')}")
    return any(w in chk for w in _words("stop")), bool(_WANTED_RE.search(t))


JUNK_RU = {"box": "коробка без товара", "incomplete": "некомплект: нет деталей", "scrap": "утиль / на запчасти",
           "accessory": "аксессуар, не сама вещь", "price": "цена-заглушка"}


# «Некомплект» имеет смысл только там, где продают устройство целиком: процессор без кулера,
# корпус без комплектующих или кулер в разделе охлаждения — это обычный товар, а не мусор.
INCOMPLETE_CATEGORIES = ("", "videokarty", "pc")
# Мосты, кабели, заглушки — мусор там, где ищем сами устройства; в «Комплектующих» и «Охлаждении» это обычный товар.
ACCESSORY_NORMAL = ("komplektuyuschie", "sistemy_ohlazhdeniya", "korpusa")


def junk_reason(title: str, description: str = "", category: str = "") -> str:
    """Причина, по которой объявление — мусор (коробка, некомплект, утиль), или ''. Правила — в words/junk.txt.
    Оговорки вроде «можно и на запчасти» у рабочей вещи мусором не считаются."""
    t = _clean_for_flags((title or "").lower().replace("ё", "е"))
    full = _clean_for_flags(f"{(title or '').lower()} {(description or '').lower()}".replace("ё", "е"))
    for reason, where, rx in _words("junk"):
        if reason == "incomplete" and category not in INCOMPLETE_CATEGORIES:
            continue
        if reason == "accessory" and category in ACCESSORY_NORMAL:
            continue
        if rx.search(t if where == "title" else full):
            return reason
    return ""


# «Новая» должно относиться к самой вещи. «Купил новую», «новая термопаста», «перешёл на новую» — это б/у.
_NOT_NEW_RE = re.compile(
    r"(?:как|почти|практически|будто|словно|состояни\w*|выглядит)\s+(?:как\s+)?нов\w+"
    r"|нов\w+\s+(?:термо\w+|прокладк\w+|кулер\w*|вентилятор\w*|вертушк\w+|охлаждени\w+|систем\w+|владел\w+|поколени\w+|"
    r"сборк\w+|пк|компьютер\w*|блок\w*|драйвер\w*|игр\w*|модел\w+|корпус\w*|плат\w+|процессор\w*|монитор\w*|год\w*)"
    r"|(?:на|к|для|от|из|с|под|взамен|вместо)\s+нов\w+")
_NEW_RE = re.compile(r"\bнов(?:ая|ый|ое|ые)\b|запечатан|нераспакован|не\s+распакован|в\s+(?:заводской\s+)?упаковке|"
                     r"в\s+пл[её]нк|\bnew\b|sealed|гарантия\s+магазина|официальная\s+гарантия|"
                     r"(?<![a-z])\brtl\b")      # RTL — розничная упаковка магазина: так пишут про новую вещь


def is_new(title: str, description: str = "") -> bool:
    """Похоже ли, что продают новую вещь. «Как новая», «купил новую», «новая термопаста» — это б/у."""
    text = _NOT_NEW_RE.sub(" ", f"{title} {description}".lower().replace("ё", "е"))
    return bool(_NEW_RE.search(text))


_MIXED_RE = re.compile(r"(?=[a-zа-яё]*[a-z])(?=[a-zа-яё]*[а-яё])[a-zа-яё]{3,}")
OBFUSCATED = 0.25     # латинские буквы внутри русских слов — так прячут текст от автопоиска и модерации


def obfuscated(text: str) -> bool:
    """Три и больше слов вперемешку латиницей и кириллицей («B пpодaже вeликолeпная») — частый приём перекупов и мошенников."""
    return len(_MIXED_RE.findall((text or "").lower())) >= 3


def risk_flags(text: str) -> dict[str, float]:
    chk = _clean_for_flags((text or "").lower().replace("ё", "е"))
    out = {name: prob for name, rx, prob in _words("risk") if rx.search(chk)}
    if obfuscated(text):
        out["obfuscated"] = OBFUSCATED
    return out


_NO_BARGAIN_RE = re.compile(r"без торг\w*|торг\w* (?:нет|неуместен|не уместен|не предлагать|не будет)|не торгу\w+|"
                            r"торг\w* не\b|никакого торга")


def is_urgent(text: str) -> bool:
    low = _NO_BARGAIN_RE.sub(" ", (text or "").lower().replace("ё", "е"))   # «без торга» — не признак срочности
    return any(w in low for w in _words("urgent"))


def parse_listing(title: str, description: str = "", assume_gpu: bool = False) -> Parsed:
    """assume_gpu — объявление из раздела «Видеокарты»: голые цифры («1060 6gb») считаем моделью (как fuzzy)."""
    t = (title or "").lower().replace("ё", "е")
    d = (description or "").lower().replace("ё", "е")
    full = f"{t} {d}"
    p = Parsed()

    if _WANTED_RE.search(t):
        return Parsed(kind="skip", skip_reason="wanted")
    if _NOT_GPU_RE.search(t):
        return Parsed(kind="skip", skip_reason="not_gpu")
    chk = _clean_for_flags(full)
    for w in _words("stop"):
        if w in chk:
            return Parsed(kind="skip", skip_reason="defective")

    mining_t, mining_d = _mining_models(t), _mining_models(d)
    if mining_t or mining_d:
        models, source = (mining_t, "title") if mining_t else (mining_d, "description")
    else:
        models, source = _find_models(t, t), "title"
    use_desc = not _OTHER_GPU_RE.search(t)
    if not models and use_desc:
        models, source = _find_models(d, full), "description"
    if not models:
        ft, fd = _fuzzy_text(t), _fuzzy_text(d) if use_desc else ""
        ctx = f"{ft} {fd}" + (" видеокарта" if assume_gpu else "")
        models, source = (_find_models(ft, ctx) or _find_models(fd, ctx)), "fuzzy"

    if _PC_RE.search(_MOBILE_ANY.sub(" ", _MOBILE_TITLE.sub(" ", t)) if models else t):
        return Parsed(kind="pc", skip_reason="pc_not_supported_yet")
    if not models:
        return Parsed(kind="skip", skip_reason="model_not_found")
    if len(models) > 1:
        return Parsed(kind="skip", skip_reason="multi_model")

    p.model = next(iter(models))
    p.model_source = source
    p.vram = _vram(t) or _vram(d)
    p.mobile = _is_mobile(t, full, p.model)

    if p.mobile:
        p.key = f"{p.model} Mobile"          # ноутбучные чипы слабее и дешевле: в общий рынок модели не идут
    elif p.model in VRAM_VARIANTS:
        allowed = VRAM_VARIANTS[p.model]
        if p.vram in allowed:
            p.key = f"{p.model} {p.vram}GB"
        elif len(allowed) == 1:
            p.vram = next(iter(allowed))
            p.key = f"{p.model} {p.vram}GB"
        else:
            p.skip_reason = "vram_unknown"   # модель есть, оценить нельзя
            p.key = ""
    else:
        p.key = p.model

    for name, rx, prob in _words("risk"):
        if rx.search(chk):
            p.flags[name] = prob
    if obfuscated(f"{title} {description}"):
        p.flags["obfuscated"] = OBFUSCATED
    if p.key == "RX 580 8GB" and re.search(r"2048\s*sp|2048", full):
        p.key = "RX 580 2048SP"           # урезанная китайская версия: дешевле обычной RX 580, свой рынок
    if source != "title":
        p.flags["model_from_desc"] = 0.05
    stated = None if p.mobile else _vram_near(t, p.model)      # только из названия: в описании «16 ГБ» чаще про ОЗУ
    if stated and FIXED_VRAM.get(p.model, stated) != stated:
        p.flags["vram_mismatch"] = 0.25
    if is_urgent(full):
        p.info.append("urgent")
    return p


def _vram_near(text: str, model: str) -> int | None:
    """Объём видеопамяти рядом с названием модели (в описании ПК «16 ГБ» чаще про ОЗУ)."""
    num = re.search(r"\d+", model).group()
    m = re.search(rf"{num}\s*(?:ti|super|xt)?\W{{0,6}}(\d{{1,2}})\s*(?:gb|гб|g(?![a-zа-я])|г(?![а-я]))", text)
    return int(m.group(1)) if m and int(m.group(1)) in VALID_VRAM else None
