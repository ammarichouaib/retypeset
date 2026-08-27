#!/usr/bin/env python3
"""Entry point for the frozen Windows build.

A Streamlit app is a script that Streamlit's own runtime executes, not a
program with a `main()`. Freezing it therefore means shipping `app.py` as data
and starting the runtime on it from inside the executable, which is what this
file does. Three things have to be arranged first, and all three are silent
failures if they are not:

1. **Pandoc.** retypeset probes `$PANDOC` before anything else, so pointing that at
   the bundled `pandoc.exe` removes the entire "install pandoc, edit PATH, open
   a new terminal" step that stops most first-time users.
2. **Streamlit's own state.** A frozen app has no writable install directory.
   Streamlit wants one for its config and usage statistics, so it is sent to
   `%LOCALAPPDATA%\\retypeset`, and telemetry is off by default -- an offline lab
   machine should not be trying to reach a stats endpoint at all.
3. **The browser.** Nothing opens it for us here, so the launcher does, once the
   server is actually listening rather than immediately (which shows an error
   page on slower machines).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("RETYPESET_PORT", "8501"))


def base_dir() -> Path:
    """Where the bundled data actually is, frozen or not."""
    if getattr(sys, "frozen", False):
        # PyInstaller one-folder: data sits next to the executable, under
        # _internal for PyInstaller >= 6. sys._MEIPASS covers both layouts.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def free_port(start: int) -> int:
    """First free port at or after `start`.

    A second copy of the app on the same machine is a normal thing to do, and
    the failure when the port is taken is an unreadable tornado traceback.
    """
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def open_when_ready(port: int, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(f"http://localhost:{port}")
                return
        time.sleep(0.4)


def main() -> int:
    root = base_dir()
    app = root / "app.py"
    if not app.exists():
        print(f"error: app.py not found next to the executable ({root})")
        input("Press Enter to close.")
        return 2

    pandoc = root / "pandoc" / "pandoc.exe"
    if pandoc.exists():
        os.environ.setdefault("PANDOC", str(pandoc))

    home = Path(os.environ.get("LOCALAPPDATA", root)) / "retypeset"
    home.mkdir(parents=True, exist_ok=True)
    # Profiles the user derives from their own templates are written here, not
    # into the installation directory, which is read-only under Program Files.
    (home / "profiles").mkdir(exist_ok=True)
    os.environ.setdefault("RETYPESET_PROFILES", str(home / "profiles"))
    os.environ.setdefault("STREAMLIT_HOME", str(home))
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")
    os.environ.setdefault("STREAMLIT_SERVER_FILE_WATCHER_TYPE", "none")

    # Profiles and models are read relative to the package, which is read-only
    # inside Program Files. Work from a writable copy in the user's profile.
    os.chdir(root)

    port = free_port(PORT)
    threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()

    print(f"retypeset is starting on http://localhost:{port}")
    print("Close this window to stop it.")

    from streamlit.web import bootstrap  # noqa: PLC0415

    bootstrap.load_config_options(flag_options={
        "server.port": port,
        "server.headless": True,
        "browser.gatherUsageStats": False,
        "global.developmentMode": False,
    })
    bootstrap.run(str(app), False, [], {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
