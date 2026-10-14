"""Где лежат данные пользователя: база, .env, профиль Chrome, списки слов, копии.

Папка данных одна для любого способа запуска — exe, `python -m gpuflip app`, консольные команды:
%APPDATA%\\gpuflip (или GPUFLIP_HOME). Поэтому после перезапуска всё на месте и база только дополняется.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DATA_FILES = ("sources.json", ".env")
DATA_DIRS = ("words", "backups", ".chrome-profile")


def home() -> Path:
    """GPUFLIP_HOME → иначе %APPDATA%/gpuflip (на других системах ~/.gpuflip)."""
    if os.environ.get("GPUFLIP_HOME"):
        return Path(os.environ["GPUFLIP_HOME"])
    if os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "gpuflip"
    return Path.home() / ".gpuflip"


def package_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent)) / "gpuflip"


def setup_stdio() -> None:
    """Вывод не должен ронять сбор: в exe консоли нет или она в кодировке без кириллицы и значков."""
    for name in ("stdout", "stderr"):
        st = getattr(sys, name)
        try:
            if st is None:
                setattr(sys, name, open(home() / "app.log", "a", encoding="utf-8", buffering=1))
            elif not st.isatty():
                st.reconfigure(encoding="utf-8", errors="replace")
            else:
                st.reconfigure(errors="replace")
        except Exception:
            pass


def _listings(db: Path) -> int:
    """Сколько объявлений в базе: -1 — файла нет, 0 — пустая или без таблицы."""
    if not db.exists():
        return -1
    try:
        c = sqlite3.connect(str(db), timeout=5)
        try:
            return c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return 0


def adopt(src: Path, dst: Path) -> list[str]:
    """Разовый переезд данных из старого места (папка проекта) в общую папку данных.

    Переносит, только если в старом месте есть база с объявлениями, а в новом базы нет или она пустая.
    Ничего не удаляет: что мешает в новом месте — переименовывается с пометкой времени.
    """
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve() or _listings(src / "gpuflip.db") <= 0 or _listings(dst / "gpuflip.db") > 0:
        return []
    dst.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved: list[str] = []

    def aside(p: Path) -> None:
        if p.exists():
            p.rename(p.with_name(f"{p.name}.до-переезда-{stamp}"))

    c = sqlite3.connect(str(src / "gpuflip.db"), timeout=30)       # дописать журнал в сам файл базы
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    for tail in ("", "-wal", "-shm"):
        aside(dst / ("gpuflip.db" + tail))
    for tail in ("", "-wal", "-shm"):
        f = src / ("gpuflip.db" + tail)
        if f.exists():
            shutil.move(str(f), str(dst / f.name))
    moved.append("gpuflip.db")
    for name in DATA_FILES:
        if (src / name).exists():
            aside(dst / name)
            shutil.move(str(src / name), str(dst / name))
            moved.append(name)
    for name in DATA_DIRS:
        s, d = src / name, dst / name
        if not s.is_dir():
            continue
        if name == "backups" and d.is_dir():                       # копии базы складываем вместе
            for f in s.iterdir():
                if not (d / f.name).exists():
                    shutil.move(str(f), str(d / f.name))
        else:
            aside(d)
            shutil.move(str(s), str(d))
        moved.append(name)
    return moved


def enter_home(legacy: Path | None = None) -> Path:
    """Подготовить папку данных и перейти в неё. legacy — где искать данные прежних версий (папка запуска)."""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    if legacy is not None:
        try:
            moved = adopt(Path(legacy), h)
            if moved:
                print(f"Данные перенесены в {h}: {', '.join(moved)}", flush=True)
        except OSError as e:
            print(f"Не удалось перенести данные из {legacy}: {e}. Закройте все окна gpuflip и запустите ещё раз.", flush=True)
            raise SystemExit(2)
    from .guard import check_db
    bad = check_db(h / "gpuflip.db")
    if bad:
        print(f"База {h / 'gpuflip.db'} повреждена ({bad}). Ничего не записываю. Восстановите её из папки backups "
              "(последняя копия gpuflip-*.db или auto-*.db) и запустите снова.", flush=True)
        raise SystemExit(4)
    if not (h / ".env").exists():
        example = package_dir().parent / ".env.example"
        (h / ".env").write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
    os.chdir(h)
    return h
