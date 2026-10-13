"""Где лежат данные пользователя: база, .env, профиль Chrome, списки слов."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def home() -> Path:
    """GPUFLIP_HOME → иначе %APPDATA%/gpuflip для собранного exe → иначе текущая папка (запуск из проекта)."""
    if os.environ.get("GPUFLIP_HOME"):
        return Path(os.environ["GPUFLIP_HOME"])
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA") or Path.home()) / "gpuflip"
    return Path.cwd()


def package_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent)) / "gpuflip"
