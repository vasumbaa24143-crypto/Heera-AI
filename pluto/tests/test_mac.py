"""The Mac background-app setup: the launchd job file and the helper script."""
import pathlib
import plistlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mac"))

import make_plist  # noqa: E402

SCRIPT = ROOT / "mac" / "pluto.sh"


def test_job_runs_the_bot_at_login_and_restarts_it():
    job = make_plist.build("/Users/me/Heera-AI/pluto/.venv/bin/python", "/Users/me/Heera-AI/pluto",
                           "/Users/me/Library/Logs/Pluto/pluto.log")
    assert job["Label"] == "com.heeralal.pluto"
    assert job["ProgramArguments"] == ["/Users/me/Heera-AI/pluto/.venv/bin/python", "bot.py"]
    assert job["WorkingDirectory"] == "/Users/me/Heera-AI/pluto"      # where .env and pluto.db live
    assert job["RunAtLoad"] is True and job["KeepAlive"] is True
    assert job["ThrottleInterval"] == 60                              # no tight restart loop
    assert job["StandardOutPath"] == job["StandardErrorPath"]


def test_awake_wraps_the_bot_in_caffeinate():
    job = make_plist.build("/p/python", "/p", "/l/pluto.log", awake=True)
    assert job["ProgramArguments"] == ["/usr/bin/caffeinate", "-i", "/p/python", "bot.py"]


def test_written_file_is_a_valid_plist_even_with_odd_paths(tmp_path):
    out = tmp_path / "job.plist"
    make_plist.main(["--python", "/Users/a b/<x>&/python", "--workdir", "/Users/a b/<x>&",
                     "--log", "/Users/a b/log", "--out", str(out), "--awake"])
    job = plistlib.loads(out.read_bytes())
    assert job["WorkingDirectory"] == "/Users/a b/<x>&"
    assert job["ProgramArguments"][2] == "/Users/a b/<x>&/python"


def test_script_is_valid_bash_and_executable():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert SCRIPT.stat().st_mode & 0o111


def test_script_refuses_to_run_off_macos():
    if sys.platform == "darwin":
        return
    r = subprocess.run(["bash", str(SCRIPT), "status"], capture_output=True, text=True)
    assert r.returncode == 1 and "this script is for macOS" in r.stderr


def test_script_never_prints_or_copies_keys():
    text = SCRIPT.read_text()
    assert "cat \"$HERE/.env\"" not in text and "source" not in text   # .env is never read by the script
    assert 'chmod 600 "$HERE/.env"' in text                            # only the owner can read it
