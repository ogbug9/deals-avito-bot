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
    assert parse_listing("RTX 3090", "Есть дефект: один из кулеров не держит обороты").skip_reason == "defective"
    p = parse_listing("RTX 3090", "Работает стабильно, но один из кулеров плохо работает")
    assert p.ok and "issues" in p.flags
    assert "mining" in parse_listing("RTX 3070 8GB", "майнил полгода").flags


# --- мобильные модели: отдельный ключ и рынок ---
@pytest.mark.parametrize("title,desc,key", [
    ("Видеокарта RTX 3070m 8gb", "", "RTX 3070 Mobile"),
    ("RTX 3060 mobile 6gb", "", "RTX 3060 Mobile"),
    ("Видеокарта GTX 1660 Ti Max-Q", "", "GTX 1660 Ti Mobile"),
    ("Видеокарта из ноутбука RTX 3070", "", "RTX 3070 Mobile"),
    ("Видеокарта RTX 2070 8gb", "снята из ноутбука MSI, MXM", "RTX 2070 Mobile"),
    ("Видеокарта MSI RTX 3070 8gb", "", "RTX 3070"),            # «3070 msi» — не мобильная
    ("RTX 3070 Gaming X Trio", "подойдет для мощного пк", "RTX 3070"),
])
def test_mobile_models_are_separate(title, desc, key):
    p = parse_listing(title, desc)
    assert p.ok and p.key == key and p.mobile == key.endswith("Mobile")


def test_laptop_itself_is_not_a_gpu():
    assert parse_listing("Ноутбук MSI RTX 3070 Max-Q 16gb").kind == "pc"


# --- плохое описание: источник модели ---
@pytest.mark.parametrize("title,desc,assume,key,source", [
    ("Видеокарта RTX 3070 8GB", "", False, "RTX 3070", "title"),
    ("Видеокарта GTX", "продаю 1070 8gb, все отлично", False, "GTX 1070", "description"),
    ("Видеокарта rxt 3O70", "", False, "RTX 3070", "fuzzy"),
    ("Видяха джифорс 1О70", "", False, "GTX 1070", "fuzzy"),
    ("rtx 30 70 gigabyte", "", False, "RTX 3070", "fuzzy"),
    ("1060 6gb", "", True, "GTX 1060 6GB", "fuzzy"),
    ("3060ti", "", True, "RTX 3060 Ti", "fuzzy"),
])
def test_model_source(title, desc, assume, key, source):
    p = parse_listing(title, desc, assume_gpu=assume)
    assert p.ok and p.key == key and p.model_source == source


def test_bare_number_needs_category():
    assert not parse_listing("1060 6gb").ok


def test_word_files_are_editable(tmp_path, monkeypatch):
    from gpuflip import normalizer, wordlists
    monkeypatch.setenv("GPUFLIP_HOME", str(tmp_path))
    d = wordlists.ensure_user_copies()
    assert (d / "stop.txt").exists() and (d / "urgent.txt").exists()
    assert parse_listing("RTX 3070 8GB", "скрипит вентилятор").ok
    (d / "stop.txt").write_text("скрипит\n", encoding="utf-8")
    (d / "risk.txt").write_text("smoke = 0.5 : пахнет\nкривая строка\n", encoding="utf-8")
    normalizer.reload_words()
    assert parse_listing("RTX 3070 8GB", "скрипит вентилятор").skip_reason == "defective"
    assert parse_listing("RTX 3070 8GB", "немного пахнет").flags == {"smoke": 0.5}


def test_spec_stop_and_risk_words():
    assert parse_listing("RTX 3070 8GB", "сильно греется").skip_reason == "defective"
    assert parse_listing("RTX 3070 8GB", "был в ремонте").skip_reason == "defective"
    assert parse_listing("RTX 3070 8GB", "не греется, без ремонта, не вскрывалась").ok
    assert "no_warranty" in parse_listing("RTX 3070 8GB", "продаю без гарантии").flags
    assert "urgent" in parse_listing("RTX 3070 8GB", "срочно, переезд").info
    assert "urgent" in parse_listing("RTX 3070 8GB", "небольшой торг возможен").info
    for text in ("Без торга.", "Торга нет. Скидок нет.", "торг неуместен", "не торгуюсь"):
        assert "urgent" not in parse_listing("RTX 3070 8GB", text).info
    assert parse_listing("Память DDR4 16gb", "одна планка с дефектом").skip_reason == "defective"
