from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("OZON_TRACKER_DATA_DIR", BASE_DIR / "data")).expanduser().resolve()
LOG_DIR = Path(os.getenv("OZON_TRACKER_LOG_DIR", BASE_DIR / "logs")).expanduser().resolve()
DB_PATH = DATA_DIR / "price_tracker.sqlite3"
SECRET_PATH = DATA_DIR / "secret.key"
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"
HOST = "127.0.0.1"
PORT = 8765
REFRESH_INTERVAL_SECONDS = 12 * 60 * 60
TOKEN_TTL_SECONDS = 24 * 60 * 60
MAX_OZON_HTML_BYTES = 8 * 1024 * 1024

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
