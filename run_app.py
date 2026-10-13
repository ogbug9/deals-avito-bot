"""Точка входа для сборки exe (PyInstaller). Из проекта запускайте: python -m gpuflip app"""
import sys

from gpuflip.app import main

if __name__ == "__main__":
    sys.exit(main())
