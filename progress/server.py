"""Build progress page: a small local web server for tracking how the DockPilot build is going.

This is internal project tracking, not part of DockPilot itself. Run it with:

    uv run python progress/server.py            # then open http://127.0.0.1:8000/progress

Routes:
    /progress       the Build progress page (page.html filled with progress.json)
    /progress.json  the raw progress data
    /               redirects to /progress

Update progress.json at the end of each chunk; the page follows automatically.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
"""The progress/ folder, which holds page.html and progress.json."""

DATA_FILE = HERE / "progress.json"
"""The progress data: one entry per building block, plus the page's text."""

PAGE_FILE = HERE / "page.html"
"""The page template; its __DATA__ marker is replaced with the progress data."""


def load_data():
    """Read and return the progress data from progress.json as a dict."""
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def render_page():
    """Return the Build progress page as HTML, with the current progress data built in.

    The data is placed inside a <script type="application/json"> block; "</" is escaped
    so no text in the data can end that block early.
    """
    data = json.dumps(load_data(), ensure_ascii=False).replace("</", "<\\/")
    return PAGE_FILE.read_text(encoding="utf-8").replace("__DATA__", data)


class ProgressHandler(BaseHTTPRequestHandler):
    """Answers the three routes; anything else gets 404 Not Found."""

    def _send(self, status, body, content_type):
        """Send one response with the given status code, body text and content type."""
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        """Serve /progress, /progress.json, or redirect / to /progress."""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/progress")
            self.end_headers()
        elif path == "/progress":
            self._send(200, render_page(), "text/html; charset=utf-8")
        elif path == "/progress.json":
            self._send(200, json.dumps(load_data(), ensure_ascii=False, indent=2), "application/json; charset=utf-8")
        else:
            self._send(404, "Not found. Try /progress", "text/plain; charset=utf-8")

    def log_message(self, format, *args):
        """Keep the terminal quiet: no line per request."""


def main(argv=None):
    """Start the server. Options: --host (default 127.0.0.1) and --port (default 8000)."""
    parser = argparse.ArgumentParser(description="Serve the DockPilot Build progress page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), ProgressHandler)
    print(f"Build progress: http://{args.host}:{args.port}/progress  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
