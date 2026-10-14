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


def test_rolling_backup_keeps_last_copies_and_skips_damaged_base(tmp_path):
    path = tmp_path / "gpuflip.db"
    c = db.connect(str(path))
    c.close()
    made = [db.rolling_backup(str(path), "hour", keep=3, every=0, now=1_800_000_000 + i * 3600) for i in range(5)]
    assert all(made) and len(list((tmp_path / "backups").glob("hour-gpuflip-*.db"))) == 3
    assert db.rolling_backup(str(path), "hour", keep=3, every=3600) is None                    # с последней копии не прошёл час
    raw = bytearray(path.read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)
    path.write_bytes(bytes(raw))
    assert db.rolling_backup(str(path), "hour", keep=3, every=0, now=1_800_100_000) is None                      # испорченную не копируем
    assert len(list((tmp_path / "backups").glob("hour-gpuflip-*.db"))) == 3


def test_adopt_never_touches_an_existing_base(tmp_path):
    from gpuflip.paths import adopt
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    c = db.connect(str(old / "gpuflip.db"))
    c.execute("INSERT INTO listings(id, title, price, url, first_seen, last_seen, source) VALUES('1','x',1,'u',0,0,'s')")
    c.commit()
    c.close()
    db.connect(str(new / "gpuflip.db")).close()                         # пустая, но существующая база
    assert adopt(old, new) == [] and (old / "gpuflip.db").exists()


def test_second_app_is_not_started_while_the_first_process_lives(monkeypatch):
    from gpuflip import app
    app.lock_path().write_text(json.dumps({"port": 1, "pid": os.getppid()}), encoding="utf-8")   # окно не отвечает, но процесс жив
    assert app.running_instance() == 1
    app.lock_path().write_text(json.dumps({"port": 1, "pid": 999_999_9}), encoding="utf-8")       # процесса нет — можно запускаться
    assert app.running_instance() is None
    app.lock_path().unlink()


def test_damaged_base_is_replaced_by_the_latest_good_copy(tmp_path):
    h = tmp_path / "home"
    (h / "backups").mkdir(parents=True)
    c = db.connect(str(h / "gpuflip.db"))
    c.execute("INSERT INTO listings(id, title, price, url, first_seen, last_seen, source) VALUES('1','x',1,'u',0,0,'s')")
    c.commit()
    c.close()
    good = db.rolling_backup(str(h / "gpuflip.db"), "hour", keep=5, every=0)
    (h / "backups" / "hour-gpuflip-99999999-000000.db").write_bytes(b"\xff" * 8192)            # свежее, но испорченное
    raw = bytearray((h / "gpuflip.db").read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)
    (h / "gpuflip.db").write_bytes(bytes(raw))
    assert guard.check_db(h / "gpuflip.db")
    name = guard.restore_latest(h)
    assert name and name in good and guard.check_db(h / "gpuflip.db") == ""
    assert any(p.name.startswith("повреждённая-") for p in (h / "backups").iterdir())          # испорченная не удалена


def test_busy_base_is_not_mistaken_for_damaged(tmp_path, monkeypatch):
    path = tmp_path / "gpuflip.db"
    db.connect(str(path)).close()
    calls = {"n": 0}
    real = guard.sqlite3.connect

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise guard.sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(guard.sqlite3, "connect", flaky)
    monkeypatch.setattr(guard.time, "sleep", lambda s: None)
    assert guard.check_db(path) == "" and calls["n"] == 3
