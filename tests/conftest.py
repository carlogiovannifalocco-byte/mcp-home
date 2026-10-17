import socket
import threading
import time

import pytest
import uvicorn


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HOME_DB", str(tmp_path / "home.db"))


@pytest.fixture(scope="session")
def base_url():
    # One server for the whole run: the MCP session manager can only be started once per app.
    mp = pytest.MonkeyPatch()
    mp.setenv("HOUSEHOLD_TOKENS", "tok-rossi:rossi,tok-bianchi:bianchi")
    from mcp_home.server import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)
    mp.undo()


@pytest.fixture
def server(base_url):
    """The MCP endpoint."""
    return f"{base_url}/mcp"
