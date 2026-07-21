"""python -m webapp"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is importable when launched as ``python -m webapp``.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

from config import ENV_PATH, env
from webapp.app import create_app

load_dotenv(ENV_PATH)
app = create_app()


def main() -> None:
    host = env("WEB_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(env("WEB_PORT", "8080") or "8080")
    debug = (env("WEB_DEBUG", "0") or "0") in ("1", "true", "True", "yes")
    print(f"Lovense pairing web app → http://{host}:{port}/")
    print("Device owners open this URL, click Pair, and scan with Lovense Connect.")
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
