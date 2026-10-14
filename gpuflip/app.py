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
from .paths import enter_home, home, setup_stdio

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
    """Сбор в отдельном потоке: редкий полный обход и частый лёгкий цикл. Один Chrome на профиль — циклы идут
    строго по очереди. При капче интервалы растут сами и возвращаются к норме после чистых циклов."""

    def __init__(self, env_file: str, interval: float = 0.0, paused: bool = False):
        super().__init__(daemon=True, name="gpuflip-worker")
        from .schedule import RateLimiter, Scheduler
        cfg = Config.load(env_file)
        self.env_file, self.interval = env_file, interval
        self.sched = Scheduler(cfg.light_min_sec, cfg.light_max_sec, interval or cfg.full_interval_sec, cfg.light_cycle)
        self.limiter = RateLimiter(cfg.max_requests_per_hour)
        self._paused = threading.Event()
        self._wake = threading.Event()
        self._halt = threading.Event()
        if paused:
            self._paused.set()
        self.state, self.last_start, self.last_end = "idle", 0.0, 0.0
        self.last_rc: int | None = None
        self.last_kind, self.last_info, self.last_error = "", {}, ""
        try:                                      # расписание и счётчик запросов — с прошлого запуска
            from .cli import restore_schedule
            restore_schedule(self.sched, self.limiter, cfg.db_path)
        except Exception:
            pass

    # --- управление ---
    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self._wake.set()

    def run_now(self) -> None:
        self._paused.clear()
        self.sched.force("full")
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def next_at(self) -> float:
        return self.sched.next_at()

    def status(self) -> dict:
        return {"state": "paused" if self.paused and self.state != "running" else self.state,
                "paused": self.paused, "last_start": self.last_start, "last_end": self.last_end,
                "next_at": 0 if self.paused else self.sched.next_at(), "last_rc": self.last_rc,
                "last_kind": self.last_kind, "last_info": self.last_info, "last_error": self.last_error,
                "interval": self.sched.full_interval, "schedule": self.sched.status(),
                "requests_hour": self.limiter.used(), "requests_limit": self.limiter.max_per_hour}

    def status_line(self) -> str:
        s = self.status()
        if s["state"] == "running":
            return "идёт " + ("лёгкий цикл…" if self.last_kind == "light" else "полный обход…")
        if s["state"] == "captcha":
            return "нужна капча в окне Chrome"
        if s["paused"]:
            return "сбор на паузе"
        if not s["last_end"]:
            return "ещё не было обходов"
        ok = {0: "успешно", 3: "Авито ограничил доступ", 5: "ошибка сети"}.get(s["last_rc"], "ошибка")
        kind = "лёгкий цикл" if self.last_kind == "light" else "обход"
        slow = f"; защита: интервалы ×{self.sched.throttle.factor:.0f}" if self.sched.throttle.slowed else ""
        return f"последний {kind} {time.strftime('%H:%M', time.localtime(s['last_end']))}: {ok}{slow}"

    # --- цикл ---
    def run(self) -> None:
        from .cli import run_due
        while not self._halt.is_set():
            try:                              # пока приложение открыто — копия базы раз в час, 24 последние
                db.rolling_backup(str(home() / "gpuflip.db"), "hour", keep=24, every=3600)
            except Exception as e:
                _log(f"копия базы не сделана: {type(e).__name__}: {e}")
            if self.paused or self.sched.due() is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            self.last_kind = self.sched.due() or ""
            self.state, self.last_start, self.last_error = "running", time.time(), ""
            try:
                res = run_due(self.sched, self.limiter, self.env_file,
                              lambda cfg: DesktopNotifier(cfg.tg_token, cfg.tg_chat_id, out=lambda t: _log(t)),
                              on_captcha=self._on_captcha, should_stop=lambda: self._halt.is_set() or self.paused,
                              log=_log, full_interval=self.interval)
                if res:
                    self.last_kind, self.last_rc, self.last_info = res
            except Exception as e:           # поток сбора не должен умирать молча
                self.last_rc, self.last_error = 1, f"{type(e).__name__}: {e}"
                self.sched.done(self.last_kind or "full", True)
                _log(f"ошибка обхода: {self.last_error}")
            self.last_end, self.state = time.time(), "idle"

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
def _git(project: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "git не установлен"
    except subprocess.TimeoutExpired:
        return 124, "git не ответил вовремя"


def check_updates() -> dict:
    """git pull в папке проекта. База мигрирует сама при следующем старте (с копией). Ошибки — понятными словами."""
    project = Path(__file__).resolve().parent.parent

    def fail(message: str) -> dict:
        return {"ok": False, "updated": False, "message": message}
    if getattr(sys, "frozen", False):
        return fail("Собранный exe через git не обновляется: обновите папку проекта и пересоберите его командой build.bat.")
    if not (project / ".git").exists():
        return fail("Папка проекта не под git — обновлять неоткуда.")
    code, url = _git(project, "remote", "get-url", "origin", timeout=15)
    if code == 127:
        return fail("git не установлен или не найден в PATH.")
    if code != 0 or not url:
        return fail("Репозиторий не настроен: у проекта нет адреса origin. Создайте репозиторий на GitHub и выполните "
                    "git remote add origin <адрес>, затем git push -u origin main.")
    code, out = _git(project, "fetch", "origin")
    if code != 0:
        last = out.splitlines()[-1] if out else ""
        return fail(f"Репозиторий {url} недоступен: он ещё не создан, закрыт или не выполнен вход в GitHub. "
                    f"Сначала создайте его и сделайте git push. ({last})")
    _, before = _git(project, "rev-parse", "HEAD", timeout=15)
    code, out = _git(project, "pull", "--ff-only", timeout=120)
    if code != 0:
        last = out.splitlines()[-1] if out else ""
        return fail(f"Обновление не применилось: локальные изменения расходятся с репозиторием или ветка не привязана. ({last})")
    _, after = _git(project, "rev-parse", "HEAD", timeout=15)
    return {"ok": True, "updated": before != after,
            "message": "Обновлено до последней версии" if before != after else "Установлена последняя версия"}


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
    ap.add_argument("--interval", type=float, default=0, help="секунд между полными обходами (по умолчанию из .env)")
    args = ap.parse_args(argv)

    setup_stdio()
    legacy = Path.cwd()
    if running_instance():                        # второй запуск — просто показать уже открытое окно
        return 0
    h = enter_home(legacy)                        # база, .env, .chrome-profile, sources.json — всё в одной папке
    wordlists.ensure_user_copies()

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
    # своё постоянное хранилище окна: тема, вид и корзина сравнения не сбрасываются при перезапуске
    webview.start(private_mode=False, storage_path=str(h / "webview"))
    desk.quit()
    return 0
