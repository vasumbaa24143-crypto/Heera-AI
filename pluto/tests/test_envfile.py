"""pluto.env is the main keys file; .env is only a fallback."""
import os

import envfile


def _absent(monkeypatch, *names):
    for n in names:
        monkeypatch.setenv(n, "x")  # registers the variable for cleanup after the test
        monkeypatch.delenv(n)


def test_pluto_env_is_used_and_dotenv_is_ignored_when_it_exists(tmp_path, monkeypatch):
    (tmp_path / "pluto.env").write_text("PLUTO_TEST_A=from-pluto\n")
    (tmp_path / ".env").write_text("PLUTO_TEST_A=from-dotenv\nPLUTO_TEST_B=from-dotenv\n")
    monkeypatch.setattr(envfile, "HERE", tmp_path)
    _absent(monkeypatch, "PLUTO_TEST_A", "PLUTO_TEST_B")
    envfile.load()
    assert os.environ["PLUTO_TEST_A"] == "from-pluto"
    assert "PLUTO_TEST_B" not in os.environ


def test_dotenv_is_the_fallback_when_pluto_env_is_missing(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PLUTO_TEST_C=from-dotenv\n")
    monkeypatch.setattr(envfile, "HERE", tmp_path)
    _absent(monkeypatch, "PLUTO_TEST_C")
    envfile.load()
    assert os.environ["PLUTO_TEST_C"] == "from-dotenv"


def test_host_environment_wins_and_no_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "HERE", tmp_path)  # neither file exists (e.g. Railway)
    monkeypatch.setenv("PLUTO_TEST_D", "from-host")
    envfile.load()
    assert os.environ["PLUTO_TEST_D"] == "from-host"
    (tmp_path / "pluto.env").write_text("PLUTO_TEST_D=from-file\n")
    envfile.load()  # a real environment variable is never overridden by the file
    assert os.environ["PLUTO_TEST_D"] == "from-host"
