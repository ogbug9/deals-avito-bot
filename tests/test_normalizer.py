import pytest

from gpuflip.normalizer import parse_listing


@pytest.mark.parametrize("title,desc,key", [
    ("Видеокарта RTX 3060 Ti 8GB Palit", "", "RTX 3060 Ti"),
    ("GTX 1660 Super", "", "GTX 1660 Super"),
    ("gtx1660s msi", "", "GTX 1660 Super"),
    ("RTX 3060 12gb", "", "RTX 3060 12GB"),
    ("Видеокарта 3060 8 гб", "", "RTX 3060 8GB"),
    ("ртх 3070 ти", "", "RTX 3070 Ti"),
    ("RX 6700XT Sapphire", "", "RX 6700 XT"),
    ("Radeon RX 580 8Gb", "", "RX 580 8GB"),
    ("Видеокарта GTX 1080 Ti 11gb", "", "GTX 1080 Ti"),
    ("Видеокарта Palit", "продаю 3080 ti, отличное состояние", "RTX 3080 Ti"),
])
def test_models(title, desc, key):
    p = parse_listing(title, desc)
    assert p.ok and p.key == key


@pytest.mark.parametrize("title,reason", [
    ("Куплю видеокарту RTX 3060", "wanted"),
    ("Игровой ПК RTX 3060 Ti Ryzen 5", "pc_not_supported_yet"),
    ("Кулер для видеокарты RTX 3070", "not_gpu"),
    ("RTX 3060 на запчасти", "defective"),
    ("RTX 3060 / RTX 3070 обе", "multi_model"),
    ("Диван 3060", "model_not_found"),
    ("RTX 3060", "vram_unknown"),
])
def test_skips(title, reason):
    p = parse_listing(title)
    assert not p.ok
    assert reason in (p.skip_reason, p.kind) or p.skip_reason == reason


def test_number_without_context_is_not_gpu():
    assert not parse_listing("Пылесос 1080 вт").ok


def test_flags():
    p = parse_listing("RTX 3070 8GB", "майнил, не тестировал, предоплата")
    assert {"mining", "untested", "prepay"} <= set(p.flags)


# Формулировки из живой выдачи Авито (октябрь 2026)
@pytest.mark.parametrize("desc", [
    "Проверена GPU-Z и FurMark под полной нагрузкой — без артефактов, сбоев и перегрева.",
    "Не майнилась. Полностью рабочая, без вылетов и артефактов.",
    "Не майнилась, без вскрытия и ремонта. Температуры в норме, артефактов нет.",
    "Видeокapтa пoлнoстью рабочая, стaбильная, бeз aртефактов, вылетoв и других проблем.",
    "Полностью рабочая, без каких либо дефектов. Ничего не ремонтировалось.",
    "Физических дефектов нет, в майнинге не была.",
])
def test_negated_defects_are_fine(desc):
    p = parse_listing("Видеокарта RTX 3070 8GB", desc)
    assert p.ok and not p.flags


@pytest.mark.parametrize("desc", [
    "В итоге 1 карта кулера не крутятся совсем",
    "Иногда при работе выключается монитор. Сейчас, почему-то, нет картинки.",
    "Скорее всего отвалилось ядро.",
    "В целом работает, но имеются постоянные артефакты в виде зеленых пикселей",
    "Отвалы чипов ГПУ",
])
def test_real_defects_rejected(desc):
    assert parse_listing("Видеокарта RTX 3070 8GB", desc).skip_reason == "defective"


def test_issue_flag():
    p = parse_listing("RTX 3090", "Работает стабильно. Есть дефект: один из кулеров плохо работает")
    assert p.ok and "issues" in p.flags
    assert "mining" in parse_listing("RTX 3070 8GB", "майнил полгода").flags
