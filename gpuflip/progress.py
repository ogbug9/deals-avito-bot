"""Что сборщик делает прямо сейчас — для живого индикатора в интерфейсе.

Состояние пишется в память и в файл progress.json рядом с базой: так интерфейс приложения видит и свой обход,
и отдельный процесс `collect`, запущенный из командной строки.
"""
from __future__ import annotations

import json
import os
import threading
import time

from .paths import home

_state: dict = {}
_lock = threading.Lock()
STALE = 15 * 60          # запись старше — считаем, что процесс давно не работает


def _path():
    return home() / "progress.json"


def start(job: str, title: str, **kw) -> None:
    """Начало работы: job — collect | full | light | analysis; title — что показать человеку."""
    with _lock:
        _state.clear()
        _state.update(job=job, title=title, started=time.time(), pid=os.getpid(), **kw)
        _write()


def set(**kw) -> None:  # noqa: A001 — короткое имя удобнее в местах вызова
    with _lock:
        if not _state:
            _state.update(job="", title="", started=time.time(), pid=os.getpid())
        _state.update(kw, ts=time.time())
        _write()


def done(summary: str = "") -> None:
    with _lock:
        _state.update(job="", finished=time.time(), summary=summary, captcha=False, ts=time.time())
        _write()


def _write() -> None:
    try:
        _path().write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read() -> dict:
    """Последнее состояние: своё или другого процесса (collect). Пустое, если давно ничего не происходило."""
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = dict(_state)
    if not d:
        return {}
    from .guard import _alive
    if d.get("job") and (not _alive(int(d.get("pid", 0))) or time.time() - d.get("ts", d.get("started", 0)) > STALE):
        d["job"], d["stale"] = "", True          # процесс завершился, не успев отметиться
    d["own"] = d.get("pid") == os.getpid()
    return d
