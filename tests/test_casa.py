import json
import socket
import threading
import time
from datetime import date

import httpx
import pytest
import uvicorn

from casa_mcp import logic
from casa_mcp.nl import plan_from_sentence

TODAY = date(2026, 10, 17)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CASA_DB", str(tmp_path / "casa.db"))


def test_grocery_grouped_by_aisle_and_deduplicated():
    assert logic.add_grocery("h", "latte")["aisle"] == "dairy"
    assert logic.add_grocery("h", "Latte")["status"] == "already_on_list"
    logic.add_grocery("h", "mele", "1 kg")
    logic.add_grocery("h", "batteries")
    assert logic.grocery_list("h")["by_aisle"] == {"dairy": ["latte"], "other": ["batteries"], "produce": ["1 kg mele"]}
    assert logic.check_off("h", "LATTE")["status"] == "checked_off"
    assert logic.grocery_list("h")["count"] == 2


def test_households_are_isolated():
    logic.add_grocery("rossi", "pane")
    assert logic.grocery_list("bianchi")["count"] == 0


def test_dates_spoken_and_iso():
    assert logic.parse_date("tomorrow", TODAY) == "2026-10-18"
    assert logic.parse_date("tra 3 giorni", TODAY) == "2026-10-20"
    assert logic.parse_date("2026-11-01", TODAY) == "2026-11-01"
    with pytest.raises(ValueError):
        logic.parse_date("someday", TODAY)


def test_bills_upcoming_overdue_and_paid():
    logic.add_bill("h", "Enel", 80, "2026-10-10", TODAY)
    logic.add_bill("h", "Fastweb", 29.9, "in 5 days", TODAY)
    logic.add_bill("h", "Condominio", 300, "2026-12-01", TODAY)
    up = logic.upcoming_bills("h", 14, TODAY)
    assert [(b["provider"], b["overdue"]) for b in up["bills"]] == [("Enel", True), ("Fastweb", False)]
    assert up["total"] == 109.9
    assert logic.pay_bill("h", "enel")["status"] == "paid"
    assert len(logic.upcoming_bills("h", 14, TODAY)["bills"]) == 1


def test_chore_rotation_is_fair():
    for p in ("Anna", "Luca", "Sara"):
        logic.add_member("h", p)
    assert logic.whose_turn("h", "trash")["next"] == "Anna"
    logic.log_chore("h", "trash", "anna", TODAY)
    assert logic.whose_turn("h", "trash")["next"] == "Luca"
    logic.log_chore("h", "trash", "Luca", TODAY)
    logic.log_chore("h", "trash", "Sara", TODAY)
    assert logic.whose_turn("h", "trash")["next"] == "Anna"


def test_pantry_and_briefing():
    logic.add_pantry("h", "yogurt", "2026-10-16", TODAY)
    logic.add_pantry("h", "mozzarella", "tomorrow", TODAY)
    logic.add_pantry("h", "riso", "2027-01-01", TODAY)
    items = logic.pantry_expiring("h", 3, TODAY)["items"]
    assert [(i["item"], i["expired"]) for i in items] == [("yogurt", True), ("mozzarella", False)]
    logic.add_bill("h", "Enel", 80, "2026-10-10", TODAY)
    speech = logic.weekly_briefing("h", TODAY)["speech"]
    assert "already overdue (Enel)" in speech and "yogurt, mozzarella" in speech


class FakeBedrock:
    def __init__(self):
        self.request = None

    def converse(self, **kwargs):
        self.request = kwargs
        actions = [
            {"tool": "add_grocery_item", "args": {"item": "milk"}},
            {"tool": "add_bill", "args": {"provider": "Gas", "amount": 80, "due": "2026-10-23"}},
        ]
        return {"output": {"message": {"content": [{"toolUse": {"name": "plan_actions", "input": {"actions": actions}}}]}}}


def test_bedrock_planning_uses_forced_tool(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    fake = FakeBedrock()
    actions = plan_from_sentence("out of milk, gas bill 80 due friday", client=fake, today=TODAY)
    assert [a["tool"] for a in actions] == ["add_grocery_item", "add_bill"]
    assert fake.request["toolConfig"]["toolChoice"] == {"tool": {"name": "plan_actions"}}
    assert "2026-10-17" in fake.request["system"][0]["text"]


def test_bedrock_required():
    with pytest.raises(ValueError):
        plan_from_sentence("anything")


# End to end over Streamable HTTP ------------------------------------------

@pytest.fixture(scope="module")
def server():
    # One server per module: the MCP session manager can only be started once per app.
    mp = pytest.MonkeyPatch()
    mp.setenv("HOUSEHOLD_TOKENS", "tok-rossi:rossi,tok-bianchi:bianchi")
    from casa_mcp.server import app

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
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    t.join(5)
    mp.undo()


HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(url, token, method, params=None, id=1):
    h = dict(HEADERS)
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = httpx.post(url, headers=h, json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})
    return r


def call(url, token, name, args=None):
    r = rpc(url, token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_rejects_missing_or_wrong_token(server):
    assert rpc(server, None, "tools/list").status_code == 401
    assert rpc(server, "nope", "tools/list").status_code == 401


def test_tools_over_http_are_scoped_per_household(server):
    names = {t["name"] for t in rpc(server, "tok-rossi", "tools/list").json()["result"]["tools"]}
    assert {"add_grocery_item", "get_grocery_list", "whose_turn", "weekly_briefing", "do_from_sentence"} <= names
    call(server, "tok-rossi", "add_grocery_item", {"item": "pane"})
    assert call(server, "tok-rossi", "get_grocery_list")["count"] == 1
    assert call(server, "tok-bianchi", "get_grocery_list")["count"] == 0
    assert "speech" in call(server, "tok-rossi", "weekly_briefing")
