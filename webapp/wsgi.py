"""WSGI entry for production servers (gunicorn).

Use a **single** worker process: pairing session state lives in memory
(``pairing.manager``). Multiple workers would desync QR / pair status.

Example::

    gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 8 webapp.wsgi:app
"""

from __future__ import annotations

import sys
from pathlib import Path

# Project root on sys.path when launched outside the repo root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

from config import ENV_PATH
from webapp.app import create_app

load_dotenv(ENV_PATH)
app = create_app()
