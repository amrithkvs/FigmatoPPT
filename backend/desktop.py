"""Desktop entry point: run the local server and show the app in a native window.

Packaged with PyInstaller into a double-click .app — no Python, no terminal.
Set FIGMADECK_NO_WINDOW=1 to run headless (server only), used for build smoke tests.
"""

import os
import socket
import threading
import time
import urllib.request


LOADING_HTML = """
<!DOCTYPE html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head>
<body style=\"margin:0;font:15px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fafafa;color:#1a1a1a;display:flex;align-items:center;justify-content:center;height:100vh;\">
    <div style=\"text-align:center;max-width:420px;padding:24px;\">
        <div style=\"font-size:22px;font-weight:650;margin-bottom:10px;\">FigPoint</div>
        <div style=\"color:#6b7280;\">Starting local app…</div>
    </div>
</body></html>
"""

ERROR_HTML = """
<!DOCTYPE html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head>
<body style=\"margin:0;font:15px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fafafa;color:#1a1a1a;display:flex;align-items:center;justify-content:center;height:100vh;\">
    <div style=\"max-width:520px;padding:24px;\">
        <div style=\"font-size:22px;font-weight:650;margin-bottom:10px;\">FigPoint</div>
        <div style=\"margin-bottom:8px;\">The local server did not start in time.</div>
        <div style=\"color:#6b7280;\">Quit the app and reopen it. If the problem continues, rebuild the app or run it from source to inspect errors.</div>
    </div>
</body></html>
"""


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
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    if os.environ.get("FIGMADECK_NO_WINDOW"):
        print(url, flush=True)
        server.run()  # blocks in the main thread (test mode)
        return

    import webview
    window = webview.create_window("FigPoint", html=LOADING_HTML, width=880, height=1040, min_size=(680, 720))

    def _boot():
        threading.Thread(target=server.run, daemon=True).start()
        if _wait_until_up(url):
            window.load_url(url)
        else:
            window.load_html(ERROR_HTML)

    webview.start(_boot)  # blocks until the window is closed; daemon server exits with the process


if __name__ == "__main__":
    run()
