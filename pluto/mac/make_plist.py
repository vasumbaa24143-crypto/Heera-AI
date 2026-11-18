"""Write the launchd agent file that keeps Pluto running on a Mac.

Used by mac/pluto.sh. Kept in Python (plistlib) so paths with spaces or odd
characters are always written correctly.
"""
import argparse
import plistlib

LABEL = "com.heeralal.pluto"
CAFFEINATE = "/usr/bin/caffeinate"


def build(python: str, workdir: str, log: str, awake: bool = False) -> dict:
    """The launchd job: run `python bot.py` in `workdir`, at login, and restart it if it stops."""
    args = [python, "bot.py"]
    if awake:
        # caffeinate -i holds off idle sleep for as long as the bot runs.
        args = [CAFFEINATE, "-i"] + args
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": workdir,     # so .env and pluto.db are found
        "RunAtLoad": True,               # start at login
        "KeepAlive": True,               # restart if it crashes or exits
        "ThrottleInterval": 60,          # ...but at most once a minute
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--python", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--awake", action="store_true")
    a = p.parse_args(argv)
    with open(a.out, "wb") as f:
        plistlib.dump(build(a.python, a.workdir, a.log, a.awake), f)


if __name__ == "__main__":
    main()
