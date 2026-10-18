"""Voice-first behaviour: spoken replies (English and Italian), read-back before high-consequence actions,
explicit MCP annotations, prompts, "what can you do", and the 500 ms round trip the Alexa+ quick start asks for."""

import re
import statistics
import time
from datetime import date

import pytest

from mcp_home import logic, server as srv, speech
from tests.test_casa import call, rpc

TODAY = date(2026, 10, 17)  # a Saturday
IDS = re.compile(r"\b(id|bill_id|_[a-z]+|[a-z]+_[a-z_]+)\b")  # ids or snake_case tool names


def test_every_reply_has_speech_without_ids_or_tool_names(server):
    calls = [
        ("add_household_member", {"person": "Anna"}),
        ("add_grocery_item", {"item": "latte"}),
        ("get_grocery_list", {}),
        ("check_off_grocery_item", {"item": "latte"}),
        ("add_bill", {"provider": "Enel", "amount": 80, "due": "tomorrow"}),
        ("upcoming_deadlines", {}),
        ("log_chore", {"chore": "trash", "person": "Anna"}),
        ("whose_turn", {"chore": "trash"}),
        ("household_members", {}),
        ("add_pantry_item", {"item": "yogurt", "expires": "in 2 days"}),
        ("pantry_expiring", {}),
        ("mark_pantry_used", {"item": "yogurt"}),
        ("weekly_briefing", {}),
        ("what_can_you_do", {}),
        ("mark_bill_paid", {"provider": "Enel"}),
        ("remove_household_member", {"person": "Anna"}),
    ]
    for name, args in calls:
        said = call(server, "tok-rossi", name, args)["speech"]
        assert said and len(said) < 400, (name, said)
        assert not IDS.search(said), (name, said)
        assert "{" not in said, (name, said)


def test_paying_a_bill_reads_it_back_first(server):
    call(server, "tok-rossi", "add_bill", {"provider": "Gas", "amount": 80, "due": "2026-10-10"})
    ask = call(server, "tok-rossi", "mark_bill_paid", {"provider": "gas"})
    assert ask["status"] == "needs_confirmation"
    assert ask["speech"].startswith("Mark the Gas bill of 80 euro, due ") and ask["speech"].endswith("as paid?")
    assert ask["details"] == {"provider": "Gas", "amount": 80.0, "due_date": "2026-10-10"}
    # Nothing changed yet.
    assert call(server, "tok-rossi", "upcoming_deadlines", {"days": 30})["total"] == 80
    done = call(server, "tok-rossi", "mark_bill_paid", {"provider": "Gas", "confirm": True})
    assert done["status"] == "paid" and done["speech"] == "Marked the Gas bill of 80 euro as paid."
    # The read-back is single use: a second confirm does not pay anything else without a new read-back.
    assert call(server, "tok-rossi", "upcoming_deadlines", {"days": 30})["total"] == 0


def test_confirm_without_a_read_back_only_reads_back(server):
    call(server, "tok-rossi", "add_bill", {"provider": "Fastweb", "amount": 29.9, "due": "tomorrow"})
    first = call(server, "tok-rossi", "mark_bill_paid", {"provider": "Fastweb", "confirm": True})
    assert first["status"] == "needs_confirmation"
    # A read-back made for one household cannot be confirmed by another.
    call(server, "tok-bianchi", "add_bill", {"provider": "Fastweb", "amount": 10, "due": "tomorrow"})
    assert call(server, "tok-bianchi", "mark_bill_paid", {"provider": "Fastweb", "confirm": True})["status"] == "needs_confirmation"
    assert call(server, "tok-rossi", "mark_bill_paid", {"provider": "Fastweb", "confirm": True})["status"] == "paid"


def test_read_back_expires():
    logic.ask_confirmation("h", "mark_bill_paid", "Enel", {"bill_id": 1}, now=1000)
    assert logic.take_confirmation("h", "mark_bill_paid", "enel", now=1000 + logic.CONFIRM_TTL + 1) is None
    logic.ask_confirmation("h", "mark_bill_paid", "Enel", {"bill_id": 1}, now=1000)
    assert logic.take_confirmation("h", "mark_bill_paid", " ENEL ", now=1001) == {"bill_id": 1}
    assert logic.take_confirmation("h", "mark_bill_paid", "enel", now=1002) is None


def test_removing_a_member_needs_a_yes_and_unknown_people_do_not(server):
    call(server, "tok-rossi", "add_household_member", {"person": "Luca"})
    assert call(server, "tok-rossi", "remove_household_member", {"person": "Marta"})["status"] == "not_found"
    ask = call(server, "tok-rossi", "remove_household_member", {"person": "luca"})
    assert ask["speech"] == "Remove Luca from the household and the chore rotation? Their history is kept."
    assert call(server, "tok-rossi", "household_members")["members"] == ["Luca"]
    assert call(server, "tok-rossi", "remove_household_member", {"person": "Luca", "confirm": True})["status"] == "removed"


def test_sentence_plan_is_read_back_and_confirmed_without_a_second_model_call(server, monkeypatch):
    plans = []

    def fake_plan(sentence):
        plans.append(sentence)
        return [{"tool": "add_grocery_item", "args": {"item": "milk"}},
                {"tool": "add_bill", "args": {"provider": "Gas", "amount": 80, "due": "2026-12-04"}},
                {"tool": "add_bill", "args": {"provider": "Water"}},  # no amount or date: cannot be read back, dropped
                {"tool": "delete_everything", "args": {}}]

    monkeypatch.setattr(srv, "plan_from_sentence", fake_plan)
    sentence = "We're out of milk and the gas bill of 80 euro is due on 4 December"
    ask = call(server, "tok-rossi", "do_from_sentence", {"sentence": sentence})
    assert ask["status"] == "needs_confirmation"
    assert ask["speech"] == ("I'll add milk to the shopping list and record the Gas bill, 80 euro, due Friday 4 December."
                             " Shall I go ahead?")
    assert call(server, "tok-rossi", "get_grocery_list")["count"] == 0
    done = call(server, "tok-rossi", "do_from_sentence", {"sentence": sentence, "confirm": True})
    assert [a["status"] for a in done["actions"]] == ["added", "added"]
    assert done["speech"].startswith("Added milk to the shopping list")
    assert plans == [sentence]  # the confirmed call ran the stored plan, not a new one


def test_italian_household_hears_italian(server):
    assert call(server, "tok-bianchi", "set_language", {"language": "italiano"})["speech"].startswith("D'accordo")
    assert call(server, "tok-bianchi", "add_grocery_item", {"item": "latte"})["speech"] == \
        "Ho aggiunto latte alla lista, reparto latticini."
    call(server, "tok-bianchi", "add_household_member", {"person": "Giulia"})
    assert call(server, "tok-bianchi", "whose_turn", {"chore": "spazzatura"})["speech"] == \
        "Tocca a Giulia portare fuori la spazzatura."
    assert call(server, "tok-bianchi", "weekly_briefing")["speech"] == \
        "Questa settimana: 1 cosa sulla lista della spesa."
    # The other household keeps English.
    assert call(server, "tok-rossi", "add_grocery_item", {"item": "pane"})["speech"].startswith("Added pane")
    with pytest.raises(AssertionError):
        call(server, "tok-bianchi", "set_language", {"language": "klingon"})


def test_italian_dates_and_briefing():
    assert speech.say_date("2026-10-18", "it", TODAY) == "domani"
    assert speech.say_date("2026-10-23", "it", TODAY) == "venerdì 23 ottobre"
    assert speech.say_date("2026-10-23", "en", TODAY) == "Friday 23 October"
    logic.add_bill("h", "Enel", 80, "2026-10-10", TODAY)
    logic.add_pantry("h", "yogurt", "tomorrow", TODAY)
    assert logic.weekly_briefing("h", TODAY, lang="it")["speech"] == \
        "Questa settimana: 1 bolletta in scadenza per 80.00 euro, 1 già scaduta (Enel); da consumare presto: yogurt."


def test_what_can_you_do_guides_a_new_household(server):
    first = call(server, "tok-rossi", "what_can_you_do")
    assert first["household_set_up"] is False and first["speech"].endswith("tell me who lives here.")
    call(server, "tok-rossi", "add_household_member", {"person": "Anna"})
    assert call(server, "tok-rossi", "what_can_you_do")["household_set_up"] is True


def test_annotations_are_explicit(server):
    tools = {t["name"]: t for t in rpc(server, "tok-rossi", "tools/list").json()["result"]["tools"]}
    destructive = {n for n, t in tools.items() if t["annotations"].get("destructiveHint")}
    assert destructive == {"mark_bill_paid", "remove_household_member"}
    for name, tool in tools.items():
        ann = tool["annotations"]
        assert ann.get("title") and tool.get("title"), name
        assert ann.get("readOnlyHint") is True or ann.get("destructiveHint") in (True, False), name
    assert "confirm" in tools["mark_bill_paid"]["inputSchema"]["properties"]


def test_prompts_for_onboarding_and_weekly_review(server):
    prompts = {p["name"] for p in rpc(server, "tok-rossi", "prompts/list").json()["result"]["prompts"]}
    assert prompts == {"set_up_household", "weekly_review"}
    got = rpc(server, "tok-rossi", "prompts/get", {"name": "set_up_household"}).json()["result"]
    assert "set_language" in got["messages"][0]["content"]["text"]


def test_tool_calls_answer_well_under_500_ms(server):
    """The Alexa+ MCP quick start: "round-trip query response latency of less than 500 ms". Measured here over
    real HTTP on localhost, so it covers the server's own work (auth, SQLite, JSON), not the network to AWS."""
    for i in range(30):
        call(server, "tok-rossi", "add_grocery_item", {"item": f"item {i}"})
    timings = []
    for _ in range(10):
        for name in ("weekly_briefing", "get_grocery_list", "upcoming_deadlines", "whose_turn"):
            t0 = time.perf_counter()
            call(server, "tok-rossi", name, {"chore": "trash"} if name == "whose_turn" else {})
            timings.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(timings, n=20)[-1]
    assert p95 < 500, f"p95 {p95:.0f} ms"
