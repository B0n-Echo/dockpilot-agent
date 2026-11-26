"""Tests for the internal Build progress page (progress/server.py)."""

import importlib.util
import json
import threading
import urllib.request
from pathlib import Path

import pytest

SERVER_FILE = Path(__file__).resolve().parents[1] / "progress" / "server.py"
"""progress/server.py lives outside the dockpilot package, so it is loaded by path."""


def _load_server():
    """Import progress/server.py as a module and return it."""
    spec = importlib.util.spec_from_file_location("progress_server", SERVER_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load_server()


@pytest.fixture
def base_url():
    """Start the progress server on a free port in the background; yield its address."""
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ProgressHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_data_has_every_block_with_a_known_status():
    """progress.json lists all 22 blocks, each with a name, a plain summary and a known status."""
    data = server.load_data()
    assert len(data["blocks"]) == 22
    for block in data["blocks"]:
        assert block["name"] and block["what"]
        assert block["status"] in {"done", "next", "planned"}


def test_page_has_the_data_built_in():
    """The rendered page carries the progress data and no leftover __DATA__ marker."""
    html = server.render_page()
    assert "__DATA__" not in html
    assert "Safe hold" in html


def test_progress_route_serves_the_page(base_url):
    """GET /progress answers 200 with the HTML page."""
    with urllib.request.urlopen(f"{base_url}/progress") as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        assert "DockPilot build progress" in resp.read().decode("utf-8")


def test_progress_json_route_serves_the_data(base_url):
    """GET /progress.json answers with the same data as progress.json."""
    with urllib.request.urlopen(f"{base_url}/progress.json") as resp:
        assert json.loads(resp.read()) == server.load_data()


def test_root_redirects_and_unknown_is_404(base_url):
    """/ leads to the progress page; an unknown path is 404."""
    with urllib.request.urlopen(f"{base_url}/") as resp:
        assert resp.url.endswith("/progress")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"{base_url}/nope")
    assert err.value.code == 404
