"""Десктопное приложение: окно (pywebview) + встроенный сервер + фоновый сбор + значок в трее.

    python -m gpuflip app            # открыть окно
    python -m gpuflip app --minimized   # стартовать свёрнутым в трей (так запускает автозагрузка)

Только поиск и уведомления. Капчу не решает: выводит окно Chrome вперёд и зовёт человека.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import db, wordlists
from .config import Config
from .notifier import Notifier
from .paths import home, package_dir

APP_NAME = "gpuflip"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ------------------------------------------------------------------ уведомления
def toast(title: str, text: str, url: str = "") -> None:
    """Системное уведомление Windows; клик открывает url. Без winotify молча ничего не делает."""
    try:
        from winotify import Notification
        n = Notification(app_id=APP_NAME, title=title[:60], msg=text[:200], launch=url or "")
        if url:
            n.add_actions(label="Открыть объявление", launch=url)
        n.show()
    except Exception:
        pass


class DesktopNotifier(Notifier):
    """Telegram (если настроен) + уведомление Windows о каждом кандидате."""

    def send(self, text: str) -> bool:
        import re
        lines = [re.sub(r"<[^>]+>", "", l) for l in text.splitlines() if l.strip()]
        url = lines[-1] if lines and lines[-1].startswith("https://www.avito.ru/") else ""
        toast(lines[0].lstrip("🔥 ") if lines else APP_NAME, " · ".join(lines[2:4]), url)
        return super().send(text)


# ------------------------------------------------------------------ фоновый сбор
class Worker(threading.Thread):
    """Цикл сбора в отдельном потоке. Один Chrome на профиль: второй обход не начнётся, пока идёт первый."""

    def __init__(self, env_file: str, interval: float = 1200.0, paused: bool = False):
        super().__init__(daemon=True, name="gpuflip-worker")
        self.env_file, self.interval = env_file, interval
        self._paused = threading.Event()
        self._wake = threading.Event()
        self._halt = threading.Event()
        if paused:
            self._paused.set()
        self.state, self.last_start, self.last_end, self.next_at = "idle", 0.0, 0.0, 0.0
        self.last_rc: int | None = None
        self.last_error = ""

    # --- управление ---
    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self._wake.set()

    def run_now(self) -> None:
        self._paused.clear()
        self.next_at = 0.0
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def status(self) -> dict:
        return {"state": "paused" if self.paused and self.state != "running" else self.state,
                "paused": self.paused, "last_start": self.last_start, "last_end": self.last_end,
                "next_at": 0 if self.paused else self.next_at, "last_rc": self.last_rc, "last_error": self.last_error,
                "interval": self.interval}

    def status_line(self) -> str:
        s = self.status()
        if s["state"] == "running":
            return "идёт обход…"
        if s["state"] == "captcha":
            return "нужна капча в окне Chrome"
        if s["paused"]:
            return "сбор на паузе"
        if not s["last_end"]:
            return "ещё не было обходов"
        ok = {0: "успешно", 3: "Авито ограничил доступ", 5: "ошибка сети"}.get(s["last_rc"], "ошибка")
        return f"последний обход {time.strftime('%H:%M', time.localtime(s['last_end']))}: {ok}"

    # --- цикл ---
    def run(self) -> None:
        from .cli import _run_sources, source_plan
        while not self._halt.is_set():
            if self.paused or time.time() < self.next_at:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            self.state, self.last_start, self.last_error = "running", time.time(), ""
            try:
                cfg = Config.load(self.env_file)
                conn = db.connect(cfg.db_path)
                notifier = DesktopNotifier(cfg.tg_token, cfg.tg_chat_id, out=lambda t: _log(t))
                self.last_rc = _run_sources(conn, cfg, notifier, source_plan(cfg), on_captcha=self._on_captcha,
                                            should_stop=lambda: self._halt.is_set() or self.paused)
                conn.close()
            except Exception as e:           # поток сбора не должен умирать молча
                self.last_rc, self.last_error = 1, f"{type(e).__name__}: {e}"
                _log(f"ошибка обхода: {self.last_error}")
            self.last_end, self.state = time.time(), "idle"
            pause = self.interval * random.uniform(0.8, 1.3) * (4 if self.last_rc == 3 else 1)
            self.next_at = time.time() + pause

    def _on_captcha(self) -> None:
        self.state = "captcha"
        toast("Авито просит капчу", "Пройдите её в окне Chrome — сбор продолжится сам.")


def _log(text: str) -> None:
    with open(home() / "watch.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


# ------------------------------------------------------------------ автозапуск
def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    project = Path(__file__).resolve().parent.parent
    return f'cmd /c "cd /d "{project}" && start "" "{exe}" -m gpuflip app --minimized"'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False
    except ImportError:
        return False


def set_autostart(on: bool) -> bool:
    """Запись в HKCU\\…\\Run — только по переключателю в настройках."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
    return autostart_enabled()


# ------------------------------------------------------------------ обновление
def check_updates() -> dict:
    """git pull в папке проекта. База мигрирует сама при следующем старте (с копией)."""
    project = Path(__file__).resolve().parent.parent
    if getattr(sys, "frozen", False) or not (project / ".git").exists():
        return {"ok": False, "updated": False, "message": "Обновление через git доступно только при запуске из папки проекта."}
    try:
        before = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip()
        r = subprocess.run(["git", "-C", str(project), "pull", "--ff-only"], capture_output=True, text=True, timeout=120,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        after = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "updated": False, "message": f"git не ответил: {e}"}
    msg = (r.stdout + r.stderr).strip().splitlines()
    return {"ok": r.returncode == 0, "updated": r.returncode == 0 and before != after,
            "message": (msg[-1] if msg else "") or ("Обновлено" if before != after else "Уже последняя версия")}


def restart() -> None:
    args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "gpuflip", "app"]
    subprocess.Popen(args, cwd=str(Path(__file__).resolve().parent.parent) if not getattr(sys, "frozen", False) else None)


# ------------------------------------------------------------------ один экземпляр
def lock_path() -> Path:
    return home() / "app.lock"


def running_instance() -> int | None:
    """Порт уже запущенного приложения (и просьба показать окно) или None."""
    try:
        port = int(json.loads(lock_path().read_text(encoding="utf-8"))["port"])
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/app", data=b'{"action":"show"}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            return port if r.status == 200 else None
    except Exception:
        return None


# ------------------------------------------------------------------ приложение
class Desktop:
    def __init__(self, worker: Worker):
        self.worker, self.window, self.tray = worker, None, None
        self.quitting = False

    # вызывается из HTTP-обработчика (ui.App.app_action)
    def action(self, name: str) -> dict:
        if name == "show":
            self.show()
        elif name == "pause":
            self.worker.pause()
        elif name == "resume":
            self.worker.resume()
        elif name == "run_now":
            self.worker.run_now()
        elif name in ("autostart_on", "autostart_off"):
            set_autostart(name == "autostart_on")
        elif name == "check_updates":
            res = check_updates()
            if res["updated"]:
                threading.Timer(1.0, lambda: (restart(), self.quit())).start()
                res["message"] += " — перезапускаюсь"
            return {**self.status(), "update": res}
        elif name == "quit":
            threading.Timer(0.3, self.quit).start()
        else:
            raise ValueError(f"неизвестное действие: {name}")
        return self.status()

    def status(self) -> dict:
        return {"desktop": True, **self.worker.status(), "status_line": self.worker.status_line(),
                "autostart": autostart_enabled(), "home": str(home())}

    def show(self) -> None:
        if self.window:
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass

    def quit(self) -> None:
        self.quitting = True
        self.worker.stop()
        try:
            lock_path().unlink(missing_ok=True)
        except OSError:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    def on_closing(self):
        """Крестик сворачивает в трей: сбор продолжается."""
        if self.quitting:
            return True
        self.window.hide()
        return False

    def start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(10, 132, 255, 255))
        d.rectangle((18, 24, 46, 40), outline="white", width=4)
        d.line((24, 40, 24, 48), fill="white", width=4)
        d.line((40, 40, 40, 48), fill="white", width=4)
        menu = pystray.Menu(
            pystray.MenuItem("Открыть окно", lambda: self.show(), default=True),
            pystray.MenuItem(lambda _: "Продолжить сбор" if self.worker.paused else "Пауза",
                             lambda: self.worker.resume() if self.worker.paused else self.worker.pause()),
            pystray.MenuItem("Обойти сейчас", lambda: self.worker.run_now()),
            pystray.MenuItem(lambda _: self.worker.status_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda: self.quit()),
        )
        self.tray = pystray.Icon(APP_NAME, img, "gpuflip", menu)
        self.tray.run_detached()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="gpuflip app")
    ap.add_argument("--minimized", action="store_true", help="стартовать в трее, окно не показывать")
    ap.add_argument("--no-collect", action="store_true", help="сбор на паузе при старте")
    ap.add_argument("--no-window", action="store_true", help="без окна и трея: только сервер и сбор (для проверки)")
    ap.add_argument("--interval", type=float, default=1200, help="секунд между обходами")
    args = ap.parse_args(argv)

    h = home()
    h.mkdir(parents=True, exist_ok=True)
    os.chdir(h)                                   # база, .env, .chrome-profile, sources.json — всё здесь
    if not (h / ".env").exists():
        example = package_dir().parent / ".env.example"
        (h / ".env").write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
    wordlists.ensure_user_copies()

    if running_instance():                        # второй запуск — просто показать уже открытое окно
        return 0

    from .ui import App, make_handler
    cfg = Config.load(".env")
    db.connect(cfg.db_path).close()               # миграция (с копией базы) — до старта потоков
    worker = Worker(".env", args.interval, paused=args.no_collect)
    desk = Desktop(worker)
    app = App(cfg.db_path, desktop=desk)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))     # свободный порт, только локально
    port = srv.server_port
    lock_path().write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    threading.Thread(target=srv.serve_forever, daemon=True, name="gpuflip-http").start()
    worker.start()
    url = f"http://127.0.0.1:{port}/"
    print(f"gpuflip: {url}  данные: {h}", flush=True)

    if args.no_window:
        try:
            while not desk.quitting:
                time.sleep(0.5)
        except KeyboardInterrupt:
            desk.quit()
        return 0

    import webview
    desk.window = webview.create_window("gpuflip — поиск перекосов цен", url, width=1320, height=860,
                                        min_size=(900, 600), hidden=args.minimized)
    desk.window.events.closing += desk.on_closing
    desk.start_tray()
    webview.start()
    desk.quit()
    return 0
