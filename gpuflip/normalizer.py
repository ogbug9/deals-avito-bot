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
        _LISTS[kind] = {"stop": wordlists.stop_words, "risk": wordlists.risk_flags, "urgent": wordlists.urgent_words}[kind]()
    return _LISTS[kind]


# Продавцы пишут «без артефактов», «не майнилась» — это не признак брака. Такие обороты вырезаем
# перед поиском стоп-слов и флагов. Слова взяты из живой выдачи Авито (октябрь 2026).
_NEG_K = r"(?:артефакт|вылет|зависа|ремонт|майнинг|майнер|ферм|дефект|проблем|перегрев|сбо[ея]|отвал|нарекани|нюанс)\w*"
_NEGATIONS = [
    re.compile(rf"\b(?:без|нет|никаких|никакого|не было|ни)\s+(?:(?!{_NEG_K})[\w\-]+\s+){{0,4}}?{_NEG_K}"
               rf"(?:(?:\s*,\s*|\s+и\s+|\s+или\s+)(?:(?!{_NEG_K})[\w\-]+\s+){{0,2}}?{_NEG_K})*"),
    re.compile(rf"{_NEG_K}\s+(?:нет|отсутству\w+|не\s+(?:был\w*|участвовал\w*|использовал\w*|наблюда\w+|замечен\w*|обнаружен\w*))"),
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


def _find_models(text: str, ctx_text: str) -> set[str]:
    """Префикс (rtx/gtx/rx) принимаем сразу; голые цифры — только если рядом есть контекст видеокарты."""
    found: set[str] = set()
    has_ctx = bool(_CONTEXT_RE.search(ctx_text))
    for m in _MODEL_RE.finditer(text):
        pre = m.group("pre")
        if not pre and not has_ctx:
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


def risk_flags(text: str) -> dict[str, float]:
    chk = _clean_for_flags((text or "").lower().replace("ё", "е"))
    return {name: prob for name, rx, prob in _words("risk") if rx.search(chk)}


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

    models, source = _find_models(t, t), "title"
    if not models:
        models, source = _find_models(d, full), "description"
    if not models:
        ft, fd = _fuzzy_text(t), _fuzzy_text(d)
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
    if source != "title":
        p.flags["model_from_desc"] = 0.05
    if is_urgent(full):
        p.info.append("urgent")
    return p


def _vram_near(text: str, model: str) -> int | None:
    """Объём видеопамяти рядом с названием модели (в описании ПК «16 ГБ» чаще про ОЗУ)."""
    num = re.search(r"\d+", model).group()
    m = re.search(rf"{num}\s*(?:ti|super|xt)?\W{{0,6}}(\d{{1,2}})\s*(?:gb|гб|g(?![a-zа-я])|г(?![а-я]))", text)
    return int(m.group(1)) if m and int(m.group(1)) in VALID_VRAM else None
