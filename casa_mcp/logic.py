"""Household logic, independent of MCP so it can be tested and reused directly."""

from __future__ import annotations

import re
from datetime import date, timedelta

from .db import connect

AISLES = {
    "produce": ["apple", "banana", "lettuce", "tomato", "potato", "onion", "carrot", "lemon", "fruit", "salad",
                "mela", "mele", "banane", "insalata", "pomodor", "patate", "cipoll", "carot", "limon", "frutta"],
    "dairy": ["milk", "cheese", "yogurt", "butter", "egg", "latte", "formaggio", "burro", "uova", "mozzarella", "parmigiano"],
    "bakery": ["bread", "pane", "croissant", "cornetti", "biscott", "cookies"],
    "meat & fish": ["chicken", "beef", "pork", "fish", "salmon", "tuna", "pollo", "manzo", "pesce", "salmone", "tonno", "prosciutto"],
    "pantry": ["pasta", "rice", "riso", "flour", "farina", "oil", "olio", "sugar", "zucchero", "coffee", "caffè", "caffe", "beans", "legumi", "sale", "salt"],
    "frozen": ["frozen", "ice cream", "gelato", "surgelat"],
    "drinks": ["water", "acqua", "juice", "succo", "beer", "birra", "wine", "vino"],
    "household": ["detergent", "detersivo", "soap", "sapone", "toilet paper", "carta igienica", "sponge", "spugn"],
}


def aisle_for(item: str) -> str:
    low = item.lower()
    for aisle, words in AISLES.items():
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            return aisle
    return "other"


def parse_date(value: str, today: date | None = None) -> str:
    """Accept ISO dates or simple spoken forms: today, tomorrow, in 3 days, next week."""
    today = today or date.today()
    v = value.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        date.fromisoformat(v)
        return v
    if v in ("today", "oggi"):
        return today.isoformat()
    if v in ("tomorrow", "domani"):
        return (today + timedelta(days=1)).isoformat()
    if v in ("next week", "la prossima settimana"):
        return (today + timedelta(days=7)).isoformat()
    m = re.fullmatch(r"(?:in|tra|fra) (\d+) (?:days?|giorni)", v)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()
    raise ValueError(f"I could not understand the date '{value}'. Use YYYY-MM-DD, 'tomorrow' or 'in N days'.")


# Grocery -----------------------------------------------------------------

def add_grocery(household: str, item: str, quantity: str = "", added_by: str = "") -> dict:
    item = item.strip()
    if not item:
        raise ValueError("item is empty")
    aisle = aisle_for(item)
    with connect() as c:
        existing = c.execute(
            "SELECT id FROM grocery WHERE household=? AND lower(item)=lower(?) AND done=0", (household, item)
        ).fetchone()
        if existing:
            return {"status": "already_on_list", "item": item, "aisle": aisle}
        c.execute(
            "INSERT INTO grocery (household, item, quantity, aisle, added_by) VALUES (?,?,?,?,?)",
            (household, item, quantity, aisle, added_by),
        )
    return {"status": "added", "item": item, "aisle": aisle}


def grocery_list(household: str) -> dict:
    with connect() as c:
        rows = c.execute(
            "SELECT item, quantity, aisle FROM grocery WHERE household=? AND done=0 ORDER BY aisle, item", (household,)
        ).fetchall()
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["aisle"], []).append(f"{r['quantity']} {r['item']}".strip())
    return {"count": len(rows), "by_aisle": grouped}


def check_off(household: str, item: str) -> dict:
    with connect() as c:
        cur = c.execute(
            "UPDATE grocery SET done=1 WHERE household=? AND lower(item)=lower(?) AND done=0", (household, item.strip())
        )
    return {"status": "checked_off" if cur.rowcount else "not_found", "item": item}


# Bills -------------------------------------------------------------------

def add_bill(household: str, provider: str, amount: float, due: str, today: date | None = None) -> dict:
    if amount <= 0:
        raise ValueError("amount must be positive")
    due_date = parse_date(due, today)
    with connect() as c:
        cur = c.execute(
            "INSERT INTO bills (household, provider, amount, due_date) VALUES (?,?,?,?)",
            (household, provider.strip(), round(amount, 2), due_date),
        )
    return {"status": "added", "id": cur.lastrowid, "provider": provider, "amount": round(amount, 2), "due_date": due_date}


def pay_bill(household: str, provider: str) -> dict:
    with connect() as c:
        row = c.execute(
            "SELECT id, amount, due_date FROM bills WHERE household=? AND lower(provider)=lower(?) AND paid=0 "
            "ORDER BY due_date LIMIT 1",
            (household, provider.strip()),
        ).fetchone()
        if not row:
            return {"status": "not_found", "provider": provider}
        c.execute("UPDATE bills SET paid=1 WHERE id=?", (row["id"],))
    return {"status": "paid", "provider": provider, "amount": row["amount"], "due_date": row["due_date"]}


def upcoming_bills(household: str, days: int = 14, today: date | None = None) -> dict:
    today = today or date.today()
    horizon = (today + timedelta(days=days)).isoformat()
    with connect() as c:
        rows = c.execute(
            "SELECT provider, amount, due_date FROM bills WHERE household=? AND paid=0 AND due_date<=? ORDER BY due_date",
            (household, horizon),
        ).fetchall()
    items = [
        {**dict(r), "overdue": r["due_date"] < today.isoformat()}
        for r in rows
    ]
    return {"days": days, "total": round(sum(i["amount"] for i in items), 2), "bills": items}


# Chores ------------------------------------------------------------------

def add_member(household: str, person: str) -> dict:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO members (household, person) VALUES (?, ?)", (household, person.strip().title()))
    return {"status": "ok", "person": person.strip().title()}


def log_chore(household: str, chore: str, person: str, today: date | None = None) -> dict:
    today = today or date.today()
    person = person.strip().title()
    add_member(household, person)
    with connect() as c:
        c.execute(
            "INSERT INTO chores (household, chore, person, done_on) VALUES (?,?,?,?)",
            (household, chore.strip().lower(), person, today.isoformat()),
        )
    return {"status": "logged", "chore": chore.strip().lower(), "person": person, "date": today.isoformat()}


def whose_turn(household: str, chore: str) -> dict:
    """Fair rotation: the member who did this chore least recently (or never) is next."""
    chore = chore.strip().lower()
    with connect() as c:
        members = [r["person"] for r in c.execute("SELECT person FROM members WHERE household=? ORDER BY person", (household,))]
        last = {
            r["person"]: (r["last"], r["n"])
            for r in c.execute(
                "SELECT person, max(id) AS last, count(*) AS n FROM chores WHERE household=? AND chore=? GROUP BY person",
                (household, chore),
            )
        }
    if not members:
        return {"status": "no_members", "message": "Tell me who lives here first, for example: log that Anna took out the trash."}
    nxt = min(members, key=lambda p: (p in last, last.get(p, (0, 0))[0]))
    return {"chore": chore, "next": nxt, "history": {p: last.get(p, (None, 0))[1] for p in members}}


# Pantry ------------------------------------------------------------------

def add_pantry(household: str, item: str, expires: str, today: date | None = None) -> dict:
    exp = parse_date(expires, today)
    with connect() as c:
        c.execute("INSERT INTO pantry (household, item, expires_on) VALUES (?,?,?)", (household, item.strip(), exp))
    return {"status": "added", "item": item.strip(), "expires_on": exp}


def pantry_expiring(household: str, days: int = 3, today: date | None = None) -> dict:
    today = today or date.today()
    horizon = (today + timedelta(days=days)).isoformat()
    with connect() as c:
        rows = c.execute(
            "SELECT item, expires_on FROM pantry WHERE household=? AND used=0 AND expires_on<=? ORDER BY expires_on",
            (household, horizon),
        ).fetchall()
    return {"days": days, "items": [{**dict(r), "expired": r["expires_on"] < today.isoformat()} for r in rows]}


def use_pantry(household: str, item: str) -> dict:
    with connect() as c:
        cur = c.execute(
            "UPDATE pantry SET used=1 WHERE id = (SELECT id FROM pantry WHERE household=? AND lower(item)=lower(?) "
            "AND used=0 ORDER BY expires_on LIMIT 1)",
            (household, item.strip()),
        )
    return {"status": "used" if cur.rowcount else "not_found", "item": item}


# Briefing ----------------------------------------------------------------

def weekly_briefing(household: str, today: date | None = None) -> dict:
    bills = upcoming_bills(household, 7, today)
    pantry = pantry_expiring(household, 3, today)
    groceries = grocery_list(household)
    parts = []
    if bills["bills"]:
        overdue = [b for b in bills["bills"] if b["overdue"]]
        parts.append(
            f"{len(bills['bills'])} bill(s) due this week for {bills['total']:.2f} euro"
            + (f", {len(overdue)} already overdue ({', '.join(b['provider'] for b in overdue)})" if overdue else "")
        )
    if pantry["items"]:
        parts.append("use soon: " + ", ".join(i["item"] for i in pantry["items"]))
    if groceries["count"]:
        parts.append(f"{groceries['count']} item(s) on the shopping list")
    speech = ("This week: " + "; ".join(parts) + ".") if parts else "Nothing needs your attention this week."
    return {"speech": speech, "bills": bills, "pantry": pantry, "grocery_count": groceries["count"]}
