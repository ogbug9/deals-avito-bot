"""Списки слов (стоп-слова, риск, срочность) из текстовых файлов — правятся без кода.

Сначала ищется <домашняя папка>/words/<имя>.txt, иначе берётся вариант из пакета.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .paths import home, package_dir

NAMES = ("stop", "risk", "urgent")


def path(name: str) -> Path:
    user = home() / "words" / f"{name}.txt"
    return user if user.exists() else package_dir() / "words" / f"{name}.txt"


def _lines(name: str) -> list[str]:
    out = []
    for line in path(name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower().replace("ё", "е")
        if line:
            out.append(line)
    return out


def stop_words() -> list[str]:
    return _lines("stop")


def urgent_words() -> list[str]:
    return _lines("urgent")


def risk_flags() -> list[tuple[str, re.Pattern, float]]:
    out = []
    for line in _lines("risk"):
        try:
            head, pattern = line.split(":", 1)
            name, prob = head.split("=", 1)
            out.append((name.strip(), re.compile(pattern.strip()), float(prob)))
        except (ValueError, re.error):
            continue   # кривая строка в пользовательском файле не должна ронять сбор
    return out


def ensure_user_copies() -> Path:
    """Кладёт копии списков в домашнюю папку, чтобы их было где править. Существующие не трогает."""
    d = home() / "words"
    d.mkdir(parents=True, exist_ok=True)
    for fname in [f"{name}.txt" for name in NAMES] + ["analogs.json"]:
        dst = d / fname
        if not dst.exists() and (package_dir() / "words" / fname).exists():
            shutil.copyfile(package_dir() / "words" / fname, dst)
    return d
