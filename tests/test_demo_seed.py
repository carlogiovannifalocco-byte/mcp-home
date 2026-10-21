from datetime import date

from mcp_home import demo_seed, logic

TODAY = date(2026, 10, 20)


def test_seed_makes_a_lived_in_household():
    demo_seed.seed("demo", TODAY)
    assert logic.list_members("demo")["members"] == demo_seed.MEMBERS
    assert len(logic.grocery_items("demo")) == 6
    bills = logic.upcoming_bills("demo", days=31, today=TODAY)
    assert any(b.get("overdue") for b in bills["bills"])
    assert logic.whose_turn("demo", "vacuum")["next"] == "Anna"
    assert logic.pantry_expiring("demo", days=3, today=TODAY)["items"]
    assert "Nothing needs your attention" not in logic.weekly_briefing("demo", TODAY)["speech"]


def test_seed_only_when_empty():
    assert demo_seed.seed_if_empty("demo") is True
    assert demo_seed.seed_if_empty("demo") is False
