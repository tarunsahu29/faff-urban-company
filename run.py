#!/usr/bin/env python3
"""Single entrypoint:  `python run.py`  does everything.

- creates a local .venv if missing
- installs requirements (only when they change)
- copies .env from .env.example on first run
- launches the FastAPI app and opens the browser

Flags:  --port 8137   --reinstall   --no-browser   --reload
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQS = ROOT / "requirements.txt"
STAMP = VENV / ".requirements.sha256"


def _in_target_venv() -> bool:
    try:
        return VENV_PY.exists() and Path(sys.executable).resolve() == VENV_PY.resolve()
    except OSError:
        return False


def _reqs_hash() -> str:
    return hashlib.sha256(REQS.read_bytes()).hexdigest() if REQS.exists() else ""


def _bootstrap() -> None:
    if not VENV_PY.exists():
        print("• creating virtual env (.venv) …")
        venv.create(VENV, with_pip=True)

    want = _reqs_hash()
    have = STAMP.read_text().strip() if STAMP.exists() else ""
    if want != have or "--reinstall" in sys.argv:
        print("• installing dependencies …")
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q",
                               "--disable-pip-version-check", "-r", str(REQS)])
        # Provision the browser used for the human-assisted UC login (patchright).
        print("• installing browser for login (patchright chromium) …")
        try:
            subprocess.check_call([str(VENV_PY), "-m", "patchright", "install", "chromium"])
        except Exception as exc:  # noqa: BLE001 - non-fatal; login can install later
            print(f"  (browser install skipped: {exc}; run "
                  f"'{VENV_PY} -m patchright install chromium' before logging in)")
        STAMP.write_text(want)
    else:
        print("• dependencies up to date")

    # Re-exec inside the venv so all imports resolve there.
    args = [a for a in sys.argv[1:] if a != "--reinstall"]
    os.execv(str(VENV_PY), [str(VENV_PY), str(ROOT / "run.py"), *args])


def _ensure_env() -> None:
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists() and example.exists():
        shutil.copy(example, env)
        print("• created .env from .env.example (fill in secrets as you capture them)")


def _arg(flag: str, default: str) -> str:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main() -> None:
    if not _in_target_venv():
        _bootstrap()
        return  # never reached (execv replaces the process)

    _ensure_env()
    port = int(_arg("--port", os.environ.get("PORT", "8137")))
    host = "127.0.0.1"
    url = f"http://{host}:{port}"

    if "--no-browser" not in sys.argv:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"\n  🛠️  faff Home Services  →  {url}\n")
    import uvicorn
    uvicorn.run("app.main:app", host=host, port=port,
                reload="--reload" in sys.argv, log_level="info")


if __name__ == "__main__":
    main()
