"""Where Pluto's keys live: pluto.env is the main file; .env is used only if it is missing.

Call load() before importing llm, stt or core: they read the environment at import time.
On a host like Railway neither file exists and the host's environment variables are used.
"""
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
MAIN = "pluto.env"


def load() -> None:
    main = HERE / MAIN
    if main.is_file():
        load_dotenv(main)
    else:
        load_dotenv(HERE / ".env")
