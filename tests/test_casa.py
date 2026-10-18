import json
from datetime import date

import httpx
import pytest

from mcp_home import logic
from mcp_home.nl import plan_from_sentence

TODAY = date(2026, 10, 17)


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
    assert speech == "This week: 1 bill due this week for 80.00 euro, 1 already overdue (Enel); use soon: yogurt, mozzarella."


def test_grocery_list_follows_walking_order():
    for item in ("gelato", "latte", "mele", "pane", "batteries"):
        logic.add_grocery("h", item)
    assert list(logic.grocery_list("h")["by_aisle"]) == ["produce", "bakery", "dairy", "frozen", "other"]


def test_recurring_bill_schedules_next_when_paid():
    logic.add_bill("h", "Enel", 80, "2026-01-31", TODAY, repeat="bimestrale")
    paid = logic.pay_bill("h", "enel")
    assert paid["status"] == "paid" and paid["next_due"] == "2026-03-31"
    nxt = logic.upcoming_bills("h", 400, TODAY)["bills"]
    assert [(b["provider"], b["due_date"], b.get("repeat")) for b in nxt] == [("Enel", "2026-03-31", "bimonthly")]
    logic.add_bill("h", "Netflix", 13.99, "2026-01-31", TODAY, repeat="monthly")
    assert logic.pay_bill("h", "Netflix")["next_due"] == "2026-02-28"
    with pytest.raises(ValueError):
        logic.add_bill("h", "Gas", 10, "tomorrow", TODAY, repeat="sometimes")


def test_pay_bill_by_id_is_household_scoped():
    bill = logic.add_bill("rossi", "Enel", 80, "tomorrow", TODAY)
    assert logic.pay_bill("bianchi", bill_id=bill["id"])["status"] == "not_found"
    assert logic.pay_bill("rossi", bill_id=bill["id"])["status"] == "paid"


def test_members_and_chore_overview():
    assert logic.add_member("h", "  giulia ")["person"] == "Giulia"
    assert logic.add_member("h", "Giulia")["status"] == "already_member"
    logic.add_member("h", "Marco")
    logic.log_chore("h", "Dishes", "giulia", TODAY, when="yesterday")
    logic.log_chore("h", "trash", "Marco", TODAY, when="2 days ago")
    logic.log_chore("h", "trash", "Giulia", TODAY, when="3 days ago")
    overview = {c["chore"]: c for c in logic.chore_overview("h")}
    assert overview["dishes"] == {"chore": "dishes", "last_person": "Giulia", "last_done": "2026-10-16", "next": "Marco"}
    # Rotation follows the date it was done, not the order it was logged.
    assert overview["trash"]["last_person"] == "Marco" and overview["trash"]["next"] == "Giulia"
    assert logic.remove_member("h", "marco")["status"] == "removed"
    assert logic.list_members("h") == {"count": 1, "members": ["Giulia"]}
    assert logic.whose_turn("h", "dishes")["next"] == "Giulia"
    assert logic.remove_member("h", "Marco")["status"] == "not_found"


def test_pantry_items_days_left_and_use_by_id():
    a = logic.add_pantry("h", "yogurt", "2026-10-16", TODAY)
    logic.add_pantry("h", "yogurt", "2026-10-25", TODAY)
    items = logic.pantry_items("h", TODAY)
    assert [(i["item"], i["days_left"], i["expired"]) for i in items] == [("yogurt", -1, True), ("yogurt", 8, False)]
    assert logic.use_pantry("h", item_id=items[1]["id"])["status"] == "used"
    assert logic.use_pantry("other", item_id=items[0]["id"])["status"] == "not_found"
    assert [i["expires_on"] for i in logic.pantry_items("h", TODAY)] == [a["expires_on"]]


def test_old_database_gets_recurrence_column(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE bills (id INTEGER PRIMARY KEY, household TEXT NOT NULL, provider TEXT NOT NULL, "
                  "amount REAL NOT NULL, due_date TEXT NOT NULL, paid INTEGER NOT NULL DEFAULT 0)")
        c.execute("INSERT INTO bills (household, provider, amount, due_date) VALUES ('h', 'Enel', 80, '2026-10-20')")
    monkeypatch.setenv("MCP_HOME_DB", str(path))
    assert logic.upcoming_bills("h", 14, TODAY)["bills"][0]["provider"] == "Enel"
    assert "next_due" not in logic.pay_bill("h", "Enel")


def test_transport_security_allows_public_host():
    from mcp_home.server import transport_security

    sec = transport_security("https://home.example.com")
    assert "home.example.com" in sec.allowed_hosts and "https://home.example.com" in sec.allowed_origins
    assert "localhost:*" in transport_security("http://localhost:8000").allowed_hosts


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


def test_member_tools_and_recurring_bill_over_mcp(server):
    names = {t["name"]: t for t in rpc(server, "tok-rossi", "tools/list").json()["result"]["tools"]}
    assert len(names) == 18
    assert names["household_members"]["annotations"]["readOnlyHint"] is True
    call(server, "tok-rossi", "add_household_member", {"person": "Giulia"})
    assert call(server, "tok-rossi", "household_members")["members"] == ["Giulia"]
    assert call(server, "tok-bianchi", "household_members")["members"] == []
    call(server, "tok-rossi", "add_bill", {"provider": "Fastweb", "amount": 29.9, "due": "tomorrow", "repeat": "monthly"})
    assert call(server, "tok-rossi", "mark_bill_paid", {"provider": "Fastweb"})["status"] == "needs_confirmation"
    assert "next_due" in call(server, "tok-rossi", "mark_bill_paid", {"provider": "Fastweb", "confirm": True})
    assert call(server, "tok-rossi", "remove_household_member", {"person": "giulia"})["status"] == "needs_confirmation"
    assert call(server, "tok-rossi", "remove_household_member", {"person": "giulia", "confirm": True})["status"] == "removed"


def test_negotiates_2025_11_25_protocol(server):
    params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    r = rpc(server, "tok-rossi", "initialize", params)
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
