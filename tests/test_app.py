import json
import threading
import time
import urllib.request

import pytest

from gpuflip import app as desktop


def test_autostart_command_points_to_this_project():
    cmd = desktop.autostart_command()
    assert "-m gpuflip app --minimized" in cmd and "cd /d" in cmd


def test_worker_controls_without_running():
    w = desktop.Worker(".env", interval=600, paused=True)
    assert w.status()["state"] == "paused" and w.status_line() == "сбор на паузе"
    w.resume()
    assert not w.paused and w.status_line() == "ещё не было обходов"
    w.last_end, w.last_rc = time.time(), 3
    assert "Авито ограничил доступ" in w.status_line()


def test_desktop_notifier_extracts_title_and_link(monkeypatch):
    shown = []
    monkeypatch.setattr(desktop, "toast", lambda title, text, url="": shown.append((title, text, url)))
    out = []
    n = desktop.DesktopNotifier(out=out.append)
    n.send("🔥 <b>RTX 3070</b> — 19 000 ₽\nВидеокарта RTX 3070\nРежим: ниже рынка\n• причина\nЧистыми ≈ +7 000 ₽\nhttps://www.avito.ru/x_1")
    assert shown == [("RTX 3070 — 19 000 ₽", "Режим: ниже рынка · • причина", "https://www.avito.ru/x_1")]
    assert out and "dry-run" in out[0]


def test_no_running_instance_without_lock():
    assert desktop.running_instance() is None


def test_app_headless_start_single_instance_and_quit(tmp_path, monkeypatch):
    """Приложение без окна: сервер на свободном порту, данные в домашней папке, второй экземпляр не стартует."""
    monkeypatch.setenv("GPUFLIP_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop, "set_autostart", lambda on: pytest.fail("автозапуск не должен включаться сам"))
    t = threading.Thread(target=desktop.main, args=(["--no-window", "--no-collect"],), daemon=True)
    t.start()
    lock = tmp_path / "app.lock"
    for _ in range(100):
        if lock.exists():
            break
        time.sleep(0.05)
    port = json.loads(lock.read_text(encoding="utf-8"))["port"]
    base = f"http://127.0.0.1:{port}"
    st = json.load(urllib.request.urlopen(base + "/api/app"))
    assert st["desktop"] and st["paused"] and st["home"] == str(tmp_path)
    assert (tmp_path / ".env").exists() and (tmp_path / "words" / "stop.txt").exists() and (tmp_path / "gpuflip.db").exists()
    assert b"gpuflip" in urllib.request.urlopen(base + "/").read()
    assert desktop.running_instance() == port          # второй запуск увидит первый и выйдет

    def post(action):
        req = urllib.request.Request(base + "/api/app", data=json.dumps({"action": action}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return json.load(urllib.request.urlopen(req))
    assert post("pause")["paused"] is True
    post("quit")
    t.join(timeout=5)
    assert not t.is_alive() and not lock.exists()
