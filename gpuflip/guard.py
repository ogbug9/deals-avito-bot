"""Один сборщик на профиль Chrome и проверка базы при старте.

Приложение и `collect` открывают Chrome на одном и том же `.chrome-profile`; вдвоём они выбивают друг друга
(«Target page, context or browser has been closed»). Замок — файл `collector.lock` с номером процесса:
второй сборщик не запускается, пока первый жив.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .paths import home


import re

BUSY_RE = re.compile(r"locked|busy", re.I)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def lock_path() -> Path:
    return home() / "collector.lock"


def holder() -> dict | None:
    """Кто сейчас держит сборщик: {pid, who, since} или None."""
    try:
        d = json.loads(lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if _alive(int(d.get("pid", 0))) else None


def acquire(who: str) -> None:
    """Занять сборщик. Если его держит другой живой процесс — BusyError (ничего не открываем)."""
    from .collector import BusyError
    h = holder()
    if h and int(h["pid"]) != os.getpid():
        raise BusyError(f"сборщик уже работает: {h.get('who', '?')} (процесс {h['pid']}). "
                        "Второй Chrome на том же профиле не запускаю — дождитесь окончания или закройте его")
    lock_path().write_text(json.dumps({"pid": os.getpid(), "who": who, "since": time.time()}, ensure_ascii=False),
                           encoding="utf-8")


def release() -> None:
    h = None
    try:
        h = json.loads(lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if int(h.get("pid", 0)) == os.getpid():
        try:
            lock_path().unlink()
        except OSError:
            pass


def check_db(path: str | Path) -> str:
    """'' — база цела (или её ещё нет); иначе текст ошибки. Повреждённую базу открывать на запись нельзя."""
    p = Path(path)
    if not p.exists():
        return ""
    for attempt in range(5):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
            try:
                res = c.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                c.close()
        except sqlite3.OperationalError as e:
            if BUSY_RE.search(str(e)):            # другой процесс пишет — это не повреждение, подождём
                time.sleep(2 + attempt * 2)
                continue
            return str(e)
        except sqlite3.Error as e:
            return str(e)
        return "" if res == "ok" else str(res)
    return ""                                        # всё время занята — значит, жива; повреждением не считаем


def restore_latest(home_dir: str | Path) -> str:
    """Повреждённую базу — в backups/повреждённая-<время>/, на её место — самую свежую целую копию.
    Возвращает имя восстановленной копии или '' (целых копий нет). Ничего не удаляется."""
    import shutil
    h = Path(home_dir)
    db = h / "gpuflip.db"
    copies = sorted((p for p in (h / "backups").glob("*.db") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    good = next((p for p in copies if not check_db(p) and _count(p) > 0), None)
    if good is None:
        return ""
    aside = h / "backups" / f"повреждённая-{time.strftime('%Y%m%d-%H%M%S')}"
    aside.mkdir(parents=True, exist_ok=True)
    for tail in ("", "-wal", "-shm"):
        f = h / f"gpuflip.db{tail}"
        if f.exists():
            shutil.move(str(f), str(aside / f.name))
    shutil.copy2(str(good), str(db))
    return good.name


def _count(p: Path) -> int:
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return 0
