"""История не теряется: общая папка данных, ушедшие лоты с датами, состояние между перезапусками."""
import io
import sqlite3
import sys

import pytest

from gpuflip import cli, db, paths
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.pipeline import process, recheck
from gpuflip.schedule import RateLimiter, Scheduler, dump_state, load_state
from gpuflip.ui import App

NOW = 1_800_000_000.0
DAY = 86400.0


def L(i, price, title="Видеокарта RTX 3070 8GB", **kw):
    return RawListing(id=str(i), title=title, price=price, url=f"https://www.avito.ru/x_{i}", **kw)


def q(**kw):
    return {k: [str(v)] for k, v in kw.items()}


def fill(path, n=12):
    conn = db.connect(str(path))
    process(conn, Config(), [L(i, 27000 + (i % 3) * 300, date_text="2 часа назад") for i in range(n)], "s", False,
            now=NOW, category="videokarty", region="moskva")
    return conn


# ---------------------------------------------------------------- одна папка данных
def test_home_is_one_folder_for_exe_and_python(monkeypatch, tmp_path):
    monkeypatch.delenv("GPUFLIP_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    plain = paths.home()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.home() == plain == tmp_path / "gpuflip"          # exe и запуск из проекта смотрят в одну базу


def test_adopt_moves_old_project_data_once(tmp_path):
    old, new = tmp_path / "project", tmp_path / "home"
    old.mkdir()
    new.mkdir()
    fill(old / "gpuflip.db").close()
    (old / "sources.json").write_text("[]", encoding="utf-8")
    (old / ".env").write_text("MAX_PRICE=35000\n", encoding="utf-8")
    (old / "words").mkdir()
    (old / "words" / "stop.txt").write_text("моё слово\n", encoding="utf-8")
    (old / "backups").mkdir()
    (old / "backups" / "a.db").write_text("x", encoding="utf-8")
    # в новой папке базы нет (существующую, даже пустую, переезд не трогает), но есть настройки первого запуска
    (new / ".env").write_text("# пример\n", encoding="utf-8")
    (new / "backups").mkdir()
    (new / "backups" / "b.db").write_text("y", encoding="utf-8")

    moved = paths.adopt(old, new)
    assert moved == ["gpuflip.db", "sources.json", ".env", "words", "backups"]
    assert sqlite3.connect(str(new / "gpuflip.db")).execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 12
    assert (new / ".env").read_text(encoding="utf-8") == "MAX_PRICE=35000\n"
    assert (new / "words" / "stop.txt").read_text(encoding="utf-8") == "моё слово\n"
    assert sorted(p.name for p in (new / "backups").iterdir()) == ["a.db", "b.db"]
    assert not (old / "gpuflip.db").exists()
    kept = [p.name for p in new.iterdir() if "до-переезда" in p.name]
    assert any(n.startswith(".env") for n in kept)   # ничего не удалено
    assert paths.adopt(old, new) == []                             # второй раз переносить нечего


def test_adopt_never_overwrites_a_base_with_data(tmp_path):
    old, new = tmp_path / "project", tmp_path / "home"
    old.mkdir()
    new.mkdir()
    fill(old / "gpuflip.db", 5).close()
    fill(new / "gpuflip.db", 3).close()
    assert paths.adopt(old, new) == []
    assert sqlite3.connect(str(new / "gpuflip.db")).execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3
    assert (old / "gpuflip.db").exists()
    assert paths.adopt(new, new) == [] and paths.adopt(tmp_path / "нет", new) == []


def test_output_never_crashes_the_collector(monkeypatch):
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))      # как в собранном exe
    paths.setup_stdio()
    print("⏸ Авито просит капчу. ⛔", flush=True)                                    # раньше здесь падал обход
    assert "Авито просит капчу" in raw.getvalue().decode("utf-8")
    monkeypatch.setattr(sys, "stdout", None)                                          # у оконного exe вывода нет вовсе
    paths.setup_stdio()
    print("в журнал", flush=True)
    sys.stdout.close()
    assert "в журнал" in (paths.home() / "app.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------- состояние между перезапусками
def test_schedule_and_request_counter_survive_restart(tmp_path):
    t = [NOW]
    clock = lambda: t[0]
    sched, lim = Scheduler(clock=clock, rng=lambda a, b: a), RateLimiter(45, clock=clock, sleep=lambda s: None)
    sched.done("full", False)
    sched.done("light", True)                      # капча: защита ×3
    for _ in range(7):
        lim.acquire()
    path = str(tmp_path / "g.db")
    db.connect(path).close()
    cli.save_schedule(sched, lim, path)

    t[0] += 600                                    # приложение закрыли и открыли через 10 минут
    sched2, lim2 = Scheduler(clock=clock, rng=lambda a, b: a), RateLimiter(45, clock=clock, sleep=lambda s: None)
    assert sched2.due() == "full"                  # без восстановления каждый запуск начинался бы с полного обхода
    cli.restore_schedule(sched2, lim2, path)
    assert sched2.due() is None and sched2.next_full_at == sched.next_full_at
    assert sched2.throttle.factor == 3 and lim2.used() == 7
    t[0] += 3600
    assert lim2.used() == 0 and sched2.due() == "full"
    load_state(sched2, lim2, {})                   # пустое состояние ничего не ломает
    assert dump_state(sched2, lim2)["factor"] == 3


def test_base_accumulates_across_reconnects(tmp_path):
    path = tmp_path / "g.db"
    fill(path).close()
    conn = db.connect(str(path))                   # «перезапуск»
    process(conn, Config(), [L(0, 26000), L(100, 25000)], "s", False, now=NOW + DAY, category="videokarty", region="moskva")
    rows = {r["id"]: r for r in conn.execute("SELECT id, first_seen, last_seen, first_price, price, posted_at FROM listings")}
    assert len(rows) == 13
    assert rows["0"]["first_seen"] == NOW and rows["0"]["last_seen"] == NOW + DAY          # дата первого появления не затирается
    assert (rows["0"]["first_price"], rows["0"]["price"]) == (27000, 26000) and rows["0"]["posted_at"] == NOW - 7200
    assert rows["100"]["first_seen"] == NOW + DAY and rows["5"]["last_seen"] == NOW
    assert conn.execute("SELECT COUNT(*) FROM price_history WHERE listing_id='0'").fetchone()[0] == 2


# ---------------------------------------------------------------- ушедшие лоты остаются в базе
def test_gone_listings_are_kept_with_dates_and_visible(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW + 6 * DAY)
    path = tmp_path / "g.db"
    conn = fill(path)
    process(conn, Config(), [L(i, 27000 + (i % 3) * 300) for i in range(4, 12)], "s", False, now=NOW + 5 * DAY,
            category="videokarty", region="moskva")
    # 0–3 не видели пять дней: проверяем карточки — 0 и 1 сняты, 2 жива
    states = {"https://www.avito.ru/x_0": "inactive", "https://www.avito.ru/x_1": "inactive"}
    res = recheck(conn, Config(verify_per_cycle=3, deep_per_cycle=0), lambda u: {"state": states.get(u, "active")},
                  now=NOW + 5 * DAY + 60)
    assert res["checked"] == 3 and res["inactive"] == 2
    conn.close()

    app = App(str(path))
    ids = lambda **kw: sorted(i["id"] for i in app.listings(q(per=100, **kw))["items"])
    assert ids() == [str(i) for i in sorted(map(str, range(2, 12)))] or len(ids()) == 10       # по умолчанию — только активные
    assert ids(status="inactive") == ["0", "1"]
    assert ids(status="stale") == ["3"]                             # 2 проверена и жива, 3 ещё ждёт проверки
    assert len(ids(status="all")) == 12                             # из базы ничего не удалено
    gone = app.listings(q(status="inactive", sort="gone"))["items"][0]
    assert gone["gone"] is True and gone["gone_at"] == NOW + 5 * DAY + 60 and gone["price"] == 27000
    assert gone["lifetime_days"] == pytest.approx(5 + (7200 + 60) / DAY)            # от публикации до снятия
    assert gone["first_seen"] == NOW and gone["last_seen"] == NOW
    stale = app.listings(q(status="stale"))["items"][0]
    assert stale["stale"] is True and stale["unseen_days"] == pytest.approx(6.0) and stale["gone"] is False
    f = app.facets()
    assert (f["total"], f["gone"], f["stale"], f["since"]) == (10, 2, 1, NOW)
    d = app.listing("0")
    assert d["gone"] and d["price_history"] and d["status"] == "inactive"


def test_verification_goes_oldest_first_and_skips_recently_checked(tmp_path):
    conn = fill(tmp_path / "g.db", 6)
    for i, days in enumerate((9, 7, 5, 1, 0, 0)):
        conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (NOW + 10 * DAY - days * DAY, str(i)))
    seen = []
    check = lambda u: (seen.append(u[-1]), {"state": "active"})[1]
    recheck(conn, Config(verify_per_cycle=2, deep_per_cycle=0, recheck_per_cycle=1), check, now=NOW + 10 * DAY)
    assert seen == ["0", "1"]                                       # самые давние
    recheck(conn, Config(verify_per_cycle=2, deep_per_cycle=0, recheck_per_cycle=1), check, now=NOW + 10 * DAY + 60)
    assert seen == ["0", "1", "2"]                                  # проверенных не трогаем, 3–5 видели недавно
    recheck(conn, Config(verify_per_cycle=0, deep_per_cycle=0, recheck_per_cycle=1), check, now=NOW + 20 * DAY)
    assert len(seen) == 3                                           # проверку можно выключить


def test_market_keeps_sold_prices_and_export_has_dates(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW + 2.5 * DAY)
    path = tmp_path / "g.db"
    conn = fill(path)
    for i, days in ((0, 1), (1, 2), (2, 3)):
        conn.execute("UPDATE listings SET status='inactive', gone_at=? WHERE id=?", (NOW + days * DAY, str(i)))
    conn.commit()
    conn.close()
    app = App(str(path))
    row = next(r for r in app.market(q(days=7))["rows"] if r["key"] == "RTX 3070")
    assert row["n"] == 9 and row["gone"] == 3 and row["gone_total"] == 3 and row["sold_median"] == 27300
    assert row["liq_days"] == pytest.approx(2 + 7200 / DAY) and row["liq_rough"] is True
    lines = app.export_csv(q(status="all", sort="gone")).lstrip("﻿").strip().split("\r\n")
    head = lines[0].split(";")
    for col in ("Объявление", "Впервые увидели", "Последний раз видели", "Опубликовано (оценка)", "Ушло", "Прожило дней"):
        assert col in head
    first = dict(zip(head, lines[1].split(";")))
    assert first["Объявление"] == "ушло" and first["Ушло"] and first["Прожило дней"] == "3.1" and first["Впервые увидели"]
    assert len(lines) == 13
    assert sum(1 for line in lines[1:] if ";активно;" in line) == 9
