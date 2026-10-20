"""A made-up household for the public demo, so a first visitor sees a lived-in home instead of empty lists.

Written through the same functions the MCP tools call, with dates relative to today, so the briefing, the
chore rotation and the expiry badges all look the way they would a few weeks into real use."""

from __future__ import annotations

from datetime import date, timedelta

from . import logic

MEMBERS = ["Anna", "Ben", "Chiara", "Dev"]


def seed(household: str, today: date | None = None) -> None:
    today = today or date.today()

    def day(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    for person in MEMBERS:
        logic.add_member(household, person)

    for item, qty, who in [
        ("oat milk", "2", "Anna"), ("eggs", "12", "Ben"), ("coffee beans", "", "Dev"),
        ("bananas", "6", "Chiara"), ("dish soap", "", "Ben"), ("tomatoes", "1 kg", "Anna"),
    ]:
        logic.add_grocery(household, item, qty, who)

    logic.add_bill(household, "Water", 41.50, day(-2), today, repeat="quarterly")
    logic.add_bill(household, "Electricity", 84.20, day(3), today, repeat="monthly")
    logic.add_bill(household, "Internet", 29.90, day(9), today, repeat="monthly")
    logic.add_bill(household, "Car insurance", 412.00, day(24), today, repeat="yearly")

    for chore, person, offset in [
        ("take out the trash", "Anna", -3), ("take out the trash", "Ben", -1), ("take out the trash", "Dev", -5),
        ("dishes", "Chiara", -1), ("dishes", "Dev", 0), ("dishes", "Anna", -2),
        ("vacuum", "Ben", -6), ("vacuum", "Chiara", -13),
    ]:
        logic.log_chore(household, chore, person, today, when=day(offset))

    for item, offset in [("yogurt", 0), ("spinach", 1), ("chicken thighs", 2), ("parmesan", 12)]:
        logic.add_pantry(household, item, day(offset), today)


def seed_if_empty(household: str) -> bool:
    if not logic.is_empty(household):
        return False
    seed(household)
    return True
