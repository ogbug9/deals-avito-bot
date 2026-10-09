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
