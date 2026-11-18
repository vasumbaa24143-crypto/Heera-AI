"""Exit Pluto when Telegram has gone quiet, so whatever runs it can start it again.

The bot asks Telegram for new messages in a long poll that answers at least every
~10 seconds, even when nothing is new. Each answer is a heartbeat. If none arrives for
STALL_SECONDS, polling has hung or stopped (as on 3 Nov 2026, when a network error left
the bot running but deaf), and a restart is the cure. The check runs on its own thread,
so it still works when the bot's event loop is stuck.

Exiting only helps if something restarts the bot: the macOS login item (launchd
KeepAlive) or Railway's restart policy. Messages sent meanwhile are not lost; Telegram
holds them for 24 hours and hands them over after the restart.
"""
import logging
import os
import threading
import time

log = logging.getLogger("pluto")

STALL_SECONDS = 300  # 5 minutes without any answer from Telegram
CHECK_EVERY_SECONDS = 30


class Watchdog:
    def __init__(self, stall_seconds=STALL_SECONDS, clock=time.monotonic, exit=os._exit):
        self.stall_seconds = stall_seconds
        self._clock = clock
        self._exit = exit  # os._exit, because a stuck event loop would ignore sys.exit
        self._last_beat = clock()

    def beat(self) -> None:
        """Telegram answered."""
        self._last_beat = self._clock()

    def quiet_for(self) -> float:
        return self._clock() - self._last_beat

    def check(self) -> bool:
        """True while healthy. Once Telegram has been quiet too long, log why and exit."""
        quiet = self.quiet_for()
        if quiet < self.stall_seconds:
            return True
        log.error("No answer from Telegram for %d s; exiting so Pluto is restarted.", quiet)
        for handler in logging.getLogger().handlers:
            handler.flush()
        self._exit(1)
        return False

    def start(self, every=CHECK_EVERY_SECONDS) -> threading.Thread:
        def run():
            while self.check():
                time.sleep(every)

        thread = threading.Thread(target=run, name="pluto-watchdog", daemon=True)
        thread.start()
        return thread
