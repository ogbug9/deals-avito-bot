"""Живой прогресс для интерфейса и задания сбора из интерфейса."""
import json
import os

from gpuflip import progress
from gpuflip.app import Worker


def test_progress_is_visible_and_cleared():
    progress.start("collect", "Добор по разделам", target=1000, cats={"pc": 600})
    progress.set(source="Системные блоки — Москва", page=13, captcha=True)
    d = progress.read()
    assert d["job"] == "collect" and d["page"] == 13 and d["captcha"] and d["own"] and d["cats"] == {"pc": 600}
    progress.done("добор закончен")
    d = progress.read()
    assert d["job"] == "" and d["summary"] == "добор закончен" and not d["captcha"]


def test_dead_process_progress_is_not_shown_as_running():
    progress._path().write_text(json.dumps({"job": "collect", "pid": 999_999_9, "started": 0, "ts": 0}), encoding="utf-8")
    d = progress.read()
    assert d["job"] == "" and d["stale"]


def test_worker_takes_collection_job_from_interface(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"DB_PATH={tmp_path / 'w.db'}\n", encoding="utf-8")
    w = Worker(str(env), paused=True)
    w.collect(["pc", "protsessory"], 1500)
    assert w.job == {"kind": "collect", "cats": ["pc", "protsessory"], "target": 1500}
    w.collect([], 10)                                  # слишком мало — поднимаем до разумного минимума
    assert w.job["target"] == 50
    w.stop_job()
    assert w.job is None and w._stop_job.is_set()
    w.analyse_now()
    assert w.last_analysis == 0
