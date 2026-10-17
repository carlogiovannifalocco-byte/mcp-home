"""The dashboard's JSON API: same bearer tokens as /mcp, same household isolation, same logic."""

import httpx
import pytest


def api(base_url, method, path, token="tok-rossi", **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.request(method, f"{base_url}{path}", headers=headers, **kwargs)


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_dashboard_page_is_public_and_carries_no_secrets(base_url):
    r = httpx.get(f"{base_url}/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "What Alexa will say" in r.text
    assert "tok-rossi" not in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert httpx.get(f"{base_url}/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/api/state"), ("POST", "/api/groceries"), ("POST", "/api/groceries/check"), ("POST", "/api/bills"),
     ("POST", "/api/bills/1/pay"), ("POST", "/api/chores"), ("POST", "/api/pantry"), ("POST", "/api/pantry/1/use"),
     ("POST", "/api/members"), ("DELETE", "/api/members/Anna")],
)
@pytest.mark.parametrize("token", [None, "nope"])
def test_every_api_route_requires_a_household_token(base_url, method, path, token):
    r = api(base_url, method, path, token=token, json={"item": "pane"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_token_in_query_string_is_not_accepted(base_url):
    assert httpx.get(f"{base_url}/api/state", params={"token": "tok-rossi"}).status_code == 401


def test_state_matches_mcp_view_and_lists_connect_info(base_url):
    ok(api(base_url, "POST", "/api/groceries", json={"item": "latte", "quantity": "2 l", "added_by": "Giulia"}))
    ok(api(base_url, "POST", "/api/bills", json={"provider": "Enel", "amount": "80,50", "due": "tomorrow", "repeat": "monthly"}))
    ok(api(base_url, "POST", "/api/pantry", json={"item": "mozzarella", "expires": "tomorrow"}))
    state = ok(api(base_url, "GET", "/api/state"))
    assert state["household"] == "rossi"
    assert state["groceries"][0] | {"id": 0} == {"id": 0, "item": "latte", "quantity": "2 l", "aisle": "dairy", "added_by": "Giulia"}
    assert state["bills"]["bills"][0]["amount"] == 80.5 and state["bills"]["bills"][0]["repeat"] == "monthly"
    assert state["pantry"][0]["days_left"] == 1
    assert "1 bill due this week for 80.50 euro" in state["briefing"]["speech"]
    assert "use soon: mozzarella" in state["briefing"]["speech"]
    assert state["connect"]["mcp_url"] == f"{base_url}/mcp"
    tools = {t["name"]: t["read_only"] for t in state["connect"]["tools"]}
    assert len(tools) == 16 and tools["weekly_briefing"] is True and tools["add_bill"] is False


def test_households_are_isolated_through_the_api(base_url):
    added = ok(api(base_url, "POST", "/api/bills", json={"provider": "Fastweb", "amount": 29.9, "due": "in 5 days"}))
    bill_id = added["result"]["id"]
    pantry = ok(api(base_url, "POST", "/api/pantry", json={"item": "yogurt", "expires": "in 2 days"}))
    item_id = pantry["state"]["pantry"][0]["id"]
    ok(api(base_url, "POST", "/api/groceries", json={"item": "pane"}))

    other = ok(api(base_url, "GET", "/api/state", token="tok-bianchi"))
    assert other["household"] == "bianchi"
    assert other["groceries"] == [] and other["bills"]["bills"] == [] and other["pantry"] == []
    # Another household cannot act on these rows by guessing ids or names.
    assert ok(api(base_url, "POST", f"/api/bills/{bill_id}/pay", token="tok-bianchi"))["result"]["status"] == "not_found"
    assert ok(api(base_url, "POST", f"/api/pantry/{item_id}/use", token="tok-bianchi"))["result"]["status"] == "not_found"
    assert ok(api(base_url, "POST", "/api/groceries/check", token="tok-bianchi", json={"item": "pane"}))["result"]["status"] == "not_found"

    mine = ok(api(base_url, "GET", "/api/state"))
    assert len(mine["groceries"]) == 1 and len(mine["bills"]["bills"]) == 1 and len(mine["pantry"]) == 1


def test_mutations_return_fresh_state(base_url):
    s = ok(api(base_url, "POST", "/api/groceries", json={"item": "pomodori"}))
    assert s["result"]["status"] == "added" and s["state"]["groceries"][0]["aisle"] == "produce"
    assert ok(api(base_url, "POST", "/api/groceries", json={"item": "Pomodori"}))["result"]["status"] == "already_on_list"
    s = ok(api(base_url, "POST", "/api/groceries/check", json={"item": "pomodori"}))
    assert s["result"]["status"] == "checked_off" and s["state"]["groceries"] == []

    bill = ok(api(base_url, "POST", "/api/bills", json={"provider": "Hera", "amount": 60, "due": "2026-01-15", "repeat": "bimonthly"}))
    s = ok(api(base_url, "POST", f"/api/bills/{bill['result']['id']}/pay"))
    assert s["result"]["next_due"] == "2026-03-15"

    for person in ("Giulia", "Marco"):
        ok(api(base_url, "POST", "/api/members", json={"person": person}))
    s = ok(api(base_url, "POST", "/api/chores", json={"chore": "trash", "person": "giulia", "when": "yesterday"}))
    assert s["state"]["chores"] == [{"chore": "trash", "last_person": "Giulia", "last_done": s["result"]["date"], "next": "Marco"}]
    s = ok(api(base_url, "DELETE", "/api/members/Marco"))
    assert s["state"]["members"] == ["Giulia"]

    item_id = ok(api(base_url, "POST", "/api/pantry", json={"item": "ricotta", "expires": "today"}))["state"]["pantry"][0]["id"]
    assert ok(api(base_url, "POST", f"/api/pantry/{item_id}/use"))["state"]["pantry"] == []


@pytest.mark.parametrize(
    "path,body",
    [("/api/groceries", {"item": ""}), ("/api/groceries", {"item": 42}), ("/api/groceries", {"item": "x" * 500}),
     ("/api/bills", {"provider": "Enel", "amount": -5, "due": "tomorrow"}),
     ("/api/bills", {"provider": "Enel", "amount": "lots", "due": "tomorrow"}),
     ("/api/bills", {"provider": "Enel", "amount": 5, "due": "someday"}),
     ("/api/bills", {"provider": "Enel", "amount": 5, "due": "tomorrow", "repeat": "sometimes"}),
     ("/api/pantry", {"item": "yogurt", "expires": "whenever"}), ("/api/chores", {"chore": "trash"})],
)
def test_bad_input_is_a_400_with_a_message(base_url, path, body):
    r = api(base_url, "POST", path, json=body)
    assert r.status_code == 400 and r.json()["error"]


def test_malformed_json_is_a_400(base_url):
    r = httpx.post(f"{base_url}/api/groceries", content=b"{not json",
                   headers={"Authorization": "Bearer tok-rossi", "Content-Type": "application/json"})
    assert r.status_code == 400
    r = httpx.post(f"{base_url}/api/groceries", json=["pane"], headers={"Authorization": "Bearer tok-rossi"})
    assert r.status_code == 400
