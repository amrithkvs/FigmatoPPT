"""Desktop entry point: run the local server and show the app in a native window.

Packaged with PyInstaller into a double-click .app — no Python, no terminal.
Set FIGMADECK_NO_WINDOW=1 to run headless (server only), used for build smoke tests.
"""

import os
import socket
import threading
import time
import urllib.request


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_up(url, timeout=20):
    for _ in range(int(timeout * 5)):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def run():
    import uvicorn
    from main import app  # importing here keeps startup work off module import

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    if os.environ.get("FIGMADECK_NO_WINDOW"):
        print(url, flush=True)
        server.run()  # blocks in the main thread (test mode)
        return

    threading.Thread(target=server.run, daemon=True).start()
    _wait_until_up(url)

    import webview
    webview.create_window("Figma → Deck", url, width=880, height=1040, min_size=(680, 720))
    webview.start()  # blocks until the window is closed; daemon server exits with the process


if __name__ == "__main__":
    run()
