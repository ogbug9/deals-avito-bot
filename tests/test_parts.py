import pytest

from gpuflip.parts import parse_cpu, parse_disk, parse_mb, parse_pc, parse_psu, parse_ram


@pytest.mark.parametrize("title,desc,key,gb,sticks", [
    ("Оперативная память DDR4 16gb 3200 Kingston Fury", "", "RAM DDR4 16GB", 16, 1),
    ("Kingston Fury DDR4 2x8gb 3200mhz", "", "RAM DDR4 16GB", 16, 2),
    ("ОЗУ ддр5 32гб (2х16) 6000", "", "RAM DDR5 32GB", 32, 2),
    ("Память DDR3 8 Гб для ноутбука", "", "RAM DDR3 8GB SODIMM", 8, 1),
    ("Оперативная память 16 гб", "комплект 2x8, DDR4, 2666", "RAM DDR4 16GB", 16, 2),
])
def test_ram(title, desc, key, gb, sticks):
    p = parse_ram(title, desc)
    assert p.key == key and p.qty == gb and p.detail["sticks"] == sticks and p.unit == "₽/ГБ"


def test_ram_needs_type_and_size():
    assert parse_ram("Оперативная память 16 гб") is None
    assert parse_ram("Память DDR4") is None


@pytest.mark.parametrize("title,key", [
    ("Процессор Intel Core i5-12400F", "CPU i5-12400F"),
    ("i7 9700k", "CPU i7-9700K"),
    ("Процессор AMD Ryzen 5 5600X OEM", "CPU Ryzen 5 5600X"),
    ("Ryzen 7 7800X3D", "CPU Ryzen 7 7800X3D"),
    ("Райзен 5 3600", "CPU Ryzen 5 3600"),
    ("Xeon E5-2670 v3", "CPU Xeon E5-2670 v3"),
    ("Процессор Xeon e5 2650v2", "CPU Xeon E5-2650 v2"),
    ("Intel Core Ultra 5 245K", "CPU Core Ultra 5 245K"),
    ("Pentium Gold G5400", "CPU Pentium/Celeron G5400"),
])
def test_cpu(title, key):
    assert parse_cpu(title).key == key


def test_cpu_kits_and_unknown_are_skipped():
    assert parse_cpu("Комплект i5-12400F + материнская плата") is None
    assert parse_cpu("Процессор для ПК") is None
    assert parse_cpu("i5-12400F и i7-9700K") is None


@pytest.mark.parametrize("title,key,brand", [
    ("Блок питания Deepcool PF700 700W", "PSU 700W", "deepcool"),
    ("Блок питания be quiet! 650 Вт 80+ Gold", "PSU 650W Gold", "be quiet"),
    ("Chieftec GPS-650A8", "PSU 650W", "chieftec"),
    ("Блок питания 750w bronze Cougar", "PSU 750W Bronze", "cougar"),
])
def test_psu(title, key, brand):
    p = parse_psu(title)
    assert p.key == key and p.brand == brand and p.unit == "₽/Вт"


def test_psu_rejects_laptop_adapters_and_unknown_power():
    assert parse_psu("Блок питания для ноутбука Asus 90W") is None
    assert parse_psu("Блок питания ATX") is None


@pytest.mark.parametrize("title,key", [
    ("SSD Samsung 970 EVO Plus 500Gb NVMe M.2", "SSD 512GB NVMe"),
    ("SSD Kingston A400 480 гб", "SSD 512GB SATA"),
    ("Жесткий диск 1TB Seagate", 'HDD 1TB 3.5"'),
    ("HDD 2.5 500gb Toshiba для ноутбука", 'HDD 512GB 2.5"'),
    ("WD Blue 4 ТБ жесткий диск", 'HDD 4TB 3.5"'),
])
def test_disk(title, key):
    assert parse_disk(title).key == key


def test_disk_rejects_accessories():
    assert parse_disk("Переходник для SSD M.2") is None
    assert parse_disk("Внешний HDD 1TB") is None


def test_mb():
    assert parse_mb("Материнская плата Gigabyte B550M Aorus Elite").key == "MB B550"
    assert parse_mb("Комплект B450 + Ryzen 5 3600") is None


def keys(b):
    return sorted(p.key for p in b.parts)


def test_pc_bundle_from_real_listing():
    b = parse_pc("Компьютер i5-2500 / RX 570 4 гб / 16 гб озу / SSD",
                 "Процессор: Intel Core i5-2500. Видеокарта: Gigabyte Radeon RX 570, 4 ГБ GDDR5. "
                 "Оперативная память: 16 ГБ DDR3. SSD 240 гб. Блок питания 500W. Материнская плата H61")
    assert keys(b) == ["CPU i5-2500", "MB H61", "PSU 500W", "RAM DDR3 16GB", "RX 570 4GB", "SSD 256GB SATA"]
    assert b.unknown == ["case"] and not b.reject


def test_pc_unknown_parts_are_not_priced():
    b = parse_pc("Игровой пк i7/16 gb ram/ 1660super/ssd 512", "")
    assert "GTX 1660 Super" in keys(b) and "SSD 512GB SATA" in keys(b)
    assert {"cpu", "ram", "mb", "psu", "case"} <= set(b.unknown)      # i7 без модели, ОЗУ без типа — unknown


def test_pc_absent_and_rejects():
    b = parse_pc("Игровой пк без видеокарты", "Ryzen 5 5600, B550, DDR4 16gb")
    assert "gpu" in b.absent and not any(p.kind == "gpu" for p in b.parts)
    assert parse_pc("Системный блок i5", "без блока питания").absent == ["psu"]
    assert parse_pc("Ноутбук MSI RTX 3060").reject == "laptop"
    assert parse_pc("Моноблок HP 24").reject == "monoblock"
    assert parse_pc("Mac mini M5 Pro").reject == "not_desktop"


def test_pc_ambiguous_gpu_is_unknown():
    b = parse_pc("Игровой пк RTX 5080 / 5070 + Ryzen 9 9950X3D")
    assert "gpu" in b.unknown and "CPU Ryzen 9 9950X3D" in keys(b)
