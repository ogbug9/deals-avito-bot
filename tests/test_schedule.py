"""Блок A: лёгкий цикл, защита от блокировок, лимит запросов, Telegram."""
from gpuflip import cli, db
from gpuflip.collector import RawListing
from gpuflip.config import Config
from gpuflip.notifier import Notifier
from gpuflip.pipeline import process
from gpuflip.schedule import RateLimiter, Scheduler, Throttle, is_trouble


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_rate_limiter_caps_requests_per_hour():
    c = Clock()
    lim = RateLimiter(3, clock=c, sleep=c.sleep)
    assert [lim.acquire() for _ in range(3)] == [0, 0, 0] and lim.used() == 3
    c.t += 600
    assert lim.wait_time() == 3000          # до истечения часа с первого запроса
    assert lim.acquire() == 3000 and c.t == 1000 + 3600
    assert lim.used() == 1                  # три старых запроса выпали из часового окна, остался новый


def test_throttle_slows_on_trouble_and_recovers_after_clean_cycles():
    t = Throttle(step=3, cap=16, clean_needed=3)
    assert t.report(False) == 1 and not t.slowed
    assert t.report(True) == 3 and t.report(True) == 9 and t.report(True) == 16      # потолок
    assert [t.report(False) for _ in range(3)] == [16, 16, 8]                         # три чистых — вдвое меньше
    assert t.report(True) == 16                                                       # снова капча
    for _ in range(12):
        t.report(False)
    assert t.factor == 1 and not t.slowed


def test_scheduler_full_first_then_light_and_backoff():
    c = Clock()
    s = Scheduler(180, 300, 3600, clock=c, rng=lambda a, b: a)       # без случайности: всегда нижняя граница
    assert s.due() == "full"
    s.done("full", False)
    assert s.due() is None and s.next_full_at == 1000 + 3600 * 0.9
    c.t += 180
    assert s.due() == "light"
    s.done("light", True)                                            # капча в лёгком цикле
    assert s.throttle.factor == 3 and s.next_light_at == c.t + 180 * 3
    c.t += 180
    assert s.due() is None                                           # замедлились втрое
    c.t += 360
    assert s.due() == "light"
    for _ in range(3):
        s.done("light", False)
    assert s.throttle.factor == 1.5 and s.next_light_at == c.t + 180 * 1.5
    s.configure(180, 300, 3600, light=False)
    c.t += 1000
    assert s.due() is None and s.next_at() == s.next_full_at         # лёгкий цикл выключен
    s.force("full")
    assert s.due() == "full"


def test_is_trouble():
    assert not is_trouble(0, {"captcha": 0, "blocked": 0, "empty": 0})
    assert is_trouble(3, {}) and is_trouble(5, {}) and is_trouble(0, {"captcha": 1}) and is_trouble(0, {"empty": 1})


def test_light_plan_takes_first_page_of_priority_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = cli.light_plan(Config())
    assert len(plan) == 2 and all(pages == 1 and cat == "videokarty" for _, _, pages, cat, _ in plan)
    assert len(cli.source_plan(Config())) == 10


def test_run_due_runs_light_without_recheck_and_backs_off(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text("DB_PATH=g.db\nMAX_REQUESTS_PER_HOUR=20\nLIGHT_MIN_SEC=200\n", encoding="utf-8")
    db.connect("g.db").close()
    calls = []

    def fake(conn, cfg, notifier, todo, **kw):
        calls.append((len(todo), kw["do_recheck"], kw["limiter"].max_per_hour))
        kw["info"].update(captcha=1 if len(calls) == 2 else 0, pages=len(todo), new=3, candidates=1)
        return 0
    monkeypatch.setattr(cli, "_run_sources", fake)
    c = Clock(1_800_000_000.0)
    sched = Scheduler(clock=c, rng=lambda a, b: a)
    lim = RateLimiter(45, clock=c, sleep=c.sleep)
    logs = []
    kind, rc, info = cli.run_due(sched, lim, str(env), lambda cfg: None, log=logs.append)
    assert (kind, rc) == ("full", 0) and calls[0] == (10, True, 20)
    assert list((tmp_path / "backups").glob("auto-g-*.db"))          # суточная копия перед полным обходом
    assert cli.run_due(sched, lim, str(env), lambda cfg: None) is None
    c.t += 200
    kind, rc, info = cli.run_due(sched, lim, str(env), lambda cfg: None, log=logs.append)
    assert kind == "light" and calls[1] == (2, False, 20) and info["captcha"] == 1
    assert sched.throttle.factor == 3 and any("замедляюсь" in x for x in logs)


class FakeSession:
    def __init__(self, status, body):
        self.status, self.body, self.sent = status, body, []

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json))
        outer = self

        class R:
            status_code = outer.status

            def json(self):
                return outer.body
        return R()


def test_telegram_test_button_explains_errors():
    assert Notifier().test() == (False, "не задан токен бота")
    assert Notifier("t").test() == (False, "не задан chat id")
    ok = FakeSession(200, {"ok": True})
    assert Notifier("123:abc", "42", session=ok).test() == (True, "отправлено")
    assert ok.sent[0][0].endswith("/bot123:abc/sendMessage") and ok.sent[0][1]["chat_id"] == "42"
    res = Notifier("bad", "42", session=FakeSession(401, {"description": "Unauthorized"})).test()
    assert res[0] is False and "неверный токен" in res[1] and "Unauthorized" in res[1]
    assert "Start" in Notifier("t", "1", session=FakeSession(403, {})).test()[1]


def test_alert_text_has_model_price_market_n_modes_reason_link():
    conn = db.connect(":memory:")
    items = [RawListing(str(i), "Видеокарта RTX 3070 8GB", 27000 + (i % 3) * 300, f"https://www.avito.ru/x_{i}")
             for i in range(12)]
    process(conn, Config(), items, "s", False, now=1_800_000_000.0, category="videokarty", region="moskva")
    out = []
    process(conn, Config(), [RawListing("500", "Видеокарта RTX 3070 8GB", 19000, "https://www.avito.ru/x_500", "срочно")],
            "s", False, notifier=Notifier(out=out.append), now=1_800_000_060.0, category="videokarty", region="moskva")
    text = out[0]
    for part in ("RTX 3070", "19 000 ₽", "Рынок: 27 300 ₽, n=12", "скидка 30%", "Режим: ниже рынка, срочная продажа",
                 "торг до 17 100 ₽", "Чистыми ≈", "https://www.avito.ru/x_500"):
        assert part in text, part


def test_per_category_collection_can_pick_and_order_sections():
    from gpuflip import sources
    srcs = sources.DEFAULT_SOURCES
    picked = cli.category_sources(srcs, ["materinskie_platy", "protsessory"])
    assert [s.category for s in picked][0] == "materinskie_platy" and {s.category for s in picked} == {"materinskie_platy", "protsessory"}
    assert cli.category_sources(srcs, None) == list(srcs)
    import pytest
    with pytest.raises(SystemExit):
        cli.category_sources(srcs, ["processors"])


def test_fast_collection_halves_pauses_until_a_captcha(monkeypatch, tmp_path):
    from pathlib import Path
    from gpuflip import sources
    html = (Path(__file__).parent / "fixtures" / "avito_search_2026-10.html").read_text(encoding="utf-8")

    class FakeCol:
        def __init__(self):
            self.captchas, self.calls = 0, 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, url):
            self.calls += 1
            if self.calls == 3:
                self.captchas += 1         # на третьей странице Авито попросил капчу (её прошли)
            return html.replace('data-item-id="', f'data-item-id="{self.calls}x')

    sleeps = []
    monkeypatch.setattr(cli, "_collector", lambda cfg: FakeCol())
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(cli.random, "uniform", lambda a, b: a)
    monkeypatch.setattr(sources, "load", lambda: [sources.Source("ЦП", "protsessory", "moskva")])
    conn = db.connect(str(tmp_path / "f.db"))
    cli.collect_per_category(conn, Config(), 10_000, max_page=5, fast=True)
    assert sleeps[:2] == [Config().delay_sec * 0.5] * 2 and all(s == Config().delay_sec for s in sleeps[2:]) and len(sleeps) == 4
