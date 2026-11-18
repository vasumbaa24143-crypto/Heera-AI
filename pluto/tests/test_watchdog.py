"""The watchdog that restarts Pluto when Telegram polling goes quiet - no network."""
import asyncio
import threading

import pytest
from telegram.request import HTTPXRequest

import bot
import watchdog


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Exits:
    def __init__(self):
        self.codes = []
        self.called = threading.Event()

    def __call__(self, code):
        self.codes.append(code)
        self.called.set()


def make(stall=300):
    clock, exits = Clock(), Exits()
    return watchdog.Watchdog(stall_seconds=stall, clock=clock, exit=exits), clock, exits


def test_quiet_polling_is_fine_until_the_limit_then_exits():
    dog, clock, exits = make()
    clock.t += 299
    assert dog.check() is True and exits.codes == []
    clock.t += 1
    assert dog.check() is False
    assert exits.codes == [1]


def test_each_answer_from_telegram_resets_the_clock():
    dog, clock, exits = make()
    for _ in range(10):
        clock.t += 250
        dog.beat()
        assert dog.check()
    assert exits.codes == []
    assert dog.quiet_for() == 0


def test_exit_is_logged_with_the_reason(caplog):
    dog, clock, _ = make()
    clock.t += 400
    dog.check()
    assert "No answer from Telegram for 400 s" in caplog.text


def test_the_check_runs_on_its_own_thread():
    dog, clock, exits = make(stall=0)  # already stalled
    thread = dog.start(every=0.01)
    assert exits.called.wait(2)
    thread.join(2)
    assert not thread.is_alive() and thread.daemon


def test_watchdog_is_on_by_default_with_five_minutes():
    assert watchdog.STALL_SECONDS == 300
    assert watchdog.Watchdog().stall_seconds == 300


def test_an_answer_on_the_polling_connection_is_a_heartbeat(monkeypatch):
    async def answered(self, *a, **k):
        return 200, b'{"ok":true,"result":[]}'

    monkeypatch.setattr(HTTPXRequest, "do_request", answered)
    dog, clock, _ = make()
    clock.t += 200
    req = bot.PollingRequest(dog)
    assert asyncio.run(req.do_request("https://x/getUpdates", "POST")) == (
        200, b'{"ok":true,"result":[]}')
    assert dog.quiet_for() == 0


def test_a_failed_poll_is_not_a_heartbeat(monkeypatch):
    async def broken(self, *a, **k):
        raise OSError("network down")

    monkeypatch.setattr(HTTPXRequest, "do_request", broken)
    dog, clock, _ = make()
    clock.t += 200
    with pytest.raises(OSError):
        asyncio.run(bot.PollingRequest(dog).do_request("https://x/getUpdates", "POST"))
    assert dog.quiet_for() == 200


def test_app_polls_through_the_watched_connection(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-not-a-real-token")
    dog, _, _ = make()
    app = bot.build_app(dog)
    polling, normal = app.bot._request
    assert isinstance(polling, bot.PollingRequest) and polling.dog is dog
    assert not isinstance(normal, bot.PollingRequest)  # replies don't count as heartbeats
