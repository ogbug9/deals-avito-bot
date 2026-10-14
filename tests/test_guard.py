"""Один сборщик на профиль Chrome и проверка базы при старте."""
import json
import os

import pytest

from gpuflip import db, guard
from gpuflip.collector import AvitoCollector, BusyError, FetchError


def test_second_collector_is_refused_while_the_first_is_alive():
    guard.lock_path().write_text(json.dumps({"pid": os.getppid(), "who": "app", "since": 0}), encoding="utf-8")
    with pytest.raises(BusyError) as e:
        guard.acquire("collect --per-category")
    assert "app" in str(e.value) and isinstance(e.value, FetchError)      # старые обработчики ошибок его ловят
    col = AvitoCollector()
    with pytest.raises(BusyError):
        col._start()                                                      # Chrome даже не запускается
    assert col._pw is None
    guard.lock_path().unlink()


def test_stale_lock_of_a_dead_process_is_taken_over_and_released():
    guard.lock_path().write_text(json.dumps({"pid": 999_999_9, "who": "старый", "since": 0}), encoding="utf-8")
    assert guard.holder() is None
    guard.acquire("collect")
    assert guard.holder()["pid"] == os.getpid()
    guard.release()
    assert not guard.lock_path().exists()


def test_damaged_base_is_detected(tmp_path):
    good = tmp_path / "good.db"
    c = db.connect(str(good))
    c.close()
    assert guard.check_db(good) == "" and guard.check_db(tmp_path / "нет.db") == ""
    bad = tmp_path / "bad.db"
    raw = bytearray(good.read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)                               # испорчена первая страница со схемой
    bad.write_bytes(bytes(raw))
    assert guard.check_db(bad) != ""
