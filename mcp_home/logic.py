"""Household logic, independent of MCP so it can be tested and reused directly (MCP tools and the web dashboard)."""

from __future__ import annotations

import calendar
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

# The order you walk a typical supermarket: fresh food first, frozen last so it stays cold.
WALK_ORDER = ["produce", "bakery", "meat & fish", "dairy", "pantry", "drinks", "household", "frozen", "other"]


def aisle_for(item: str) -> str:
    low = item.lower()
    for aisle, words in AISLES.items():
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            return aisle
    return "other"


def parse_date(value: str, today: date | None = None) -> str:
    """Accept ISO dates or simple spoken forms: today, tomorrow, yesterday, in 3 days, 2 days ago, next week."""
    today = today or date.today()
    v = value.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        date.fromisoformat(v)
        return v
    if v in ("today", "oggi"):
        return today.isoformat()
    if v in ("tomorrow", "domani"):
        return (today + timedelta(days=1)).isoformat()
    if v in ("yesterday", "ieri"):
        return (today - timedelta(days=1)).isoformat()
    if v in ("next week", "la prossima settimana"):
        return (today + timedelta(days=7)).isoformat()
    m = re.fullmatch(r"(?:in|tra|fra) (\d+) (?:days?|giorni)", v)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()
    m = re.fullmatch(r"(\d+) (?:days? ago|giorni fa)", v)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
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
            (household, item, quantity.strip(), aisle, added_by.strip()),
        )
    return {"status": "added", "item": item, "aisle": aisle}


def grocery_items(household: str) -> list[dict]:
    """Open shopping list items as rows, in supermarket walking order."""
    with connect() as c:
        rows = c.execute(
            "SELECT id, item, quantity, aisle, added_by FROM grocery WHERE household=? AND done=0", (household,)
        ).fetchall()
    rank = {a: i for i, a in enumerate(WALK_ORDER)}
    return sorted((dict(r) for r in rows), key=lambda r: (rank.get(r["aisle"], len(rank)), r["item"].lower()))


def grocery_list(household: str) -> dict:
    items = grocery_items(household)
    grouped: dict[str, list[str]] = {}
    for r in items:
        grouped.setdefault(r["aisle"], []).append(f"{r['quantity']} {r['item']}".strip())
    return {"count": len(items), "by_aisle": grouped}


def check_off(household: str, item: str) -> dict:
    with connect() as c:
        cur = c.execute(
            "UPDATE grocery SET done=1 WHERE household=? AND lower(item)=lower(?) AND done=0", (household, item.strip())
        )
    return {"status": "checked_off" if cur.rowcount else "not_found", "item": item}


# Bills -------------------------------------------------------------------

RECURRENCE_MONTHS = {"monthly": 1, "bimonthly": 2, "quarterly": 3, "yearly": 12}
RECURRENCE_ALIASES = {
    "": "", "none": "", "once": "", "una tantum": "",
    "weekly": "weekly", "settimanale": "weekly",
    "monthly": "monthly", "mensile": "monthly",
    "bimonthly": "bimonthly", "every two months": "bimonthly", "bimestrale": "bimonthly",
    "quarterly": "quarterly", "trimestrale": "quarterly",
    "yearly": "yearly", "annual": "yearly", "annually": "yearly", "annuale": "yearly",
}


def normalize_recurrence(value: str) -> str:
    key = (value or "").strip().lower()
    if key not in RECURRENCE_ALIASES:
        raise ValueError(f"Unknown repeat '{value}'. Use weekly, monthly, bimonthly, quarterly or yearly.")
    return RECURRENCE_ALIASES[key]


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_due(due_date: str, recurrence: str) -> str:
    d = date.fromisoformat(due_date)
    if recurrence == "weekly":
        return (d + timedelta(days=7)).isoformat()
    return _add_months(d, RECURRENCE_MONTHS[recurrence]).isoformat()


def add_bill(household: str, provider: str, amount: float, due: str, today: date | None = None,
             repeat: str = "") -> dict:
    if amount <= 0:
        raise ValueError("amount must be positive")
    provider = provider.strip()
    if not provider:
        raise ValueError("provider is empty")
    due_date = parse_date(due, today)
    recurrence = normalize_recurrence(repeat)
    with connect() as c:
        cur = c.execute(
            "INSERT INTO bills (household, provider, amount, due_date, recurrence) VALUES (?,?,?,?,?)",
            (household, provider, round(amount, 2), due_date, recurrence),
        )
    out = {"status": "added", "id": cur.lastrowid, "provider": provider, "amount": round(amount, 2), "due_date": due_date}
    if recurrence:
        out["repeat"] = recurrence
    return out


def pay_bill(household: str, provider: str = "", bill_id: int | None = None) -> dict:
    """Pay one open bill: by id, or the oldest open one from `provider`. Recurring bills schedule the next one."""
    with connect() as c:
        if bill_id is not None:
            row = c.execute(
                "SELECT id, provider, amount, due_date, recurrence FROM bills WHERE household=? AND id=? AND paid=0",
                (household, bill_id),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT id, provider, amount, due_date, recurrence FROM bills WHERE household=? AND lower(provider)=lower(?) "
                "AND paid=0 ORDER BY due_date LIMIT 1",
                (household, provider.strip()),
            ).fetchone()
        if not row:
            return {"status": "not_found", "provider": provider}
        c.execute("UPDATE bills SET paid=1 WHERE id=?", (row["id"],))
        out = {"status": "paid", "provider": row["provider"], "amount": row["amount"], "due_date": row["due_date"]}
        if row["recurrence"]:
            nxt = next_due(row["due_date"], row["recurrence"])
            c.execute(
                "INSERT INTO bills (household, provider, amount, due_date, recurrence) VALUES (?,?,?,?,?)",
                (household, row["provider"], row["amount"], nxt, row["recurrence"]),
            )
            out["next_due"] = nxt
    return out


def upcoming_bills(household: str, days: int = 14, today: date | None = None) -> dict:
    today = today or date.today()
    horizon = (today + timedelta(days=days)).isoformat()
    with connect() as c:
        rows = c.execute(
            "SELECT id, provider, amount, due_date, recurrence FROM bills WHERE household=? AND paid=0 AND due_date<=? "
            "ORDER BY due_date, id",
            (household, horizon),
        ).fetchall()
    items = []
    for r in rows:
        item = {"id": r["id"], "provider": r["provider"], "amount": r["amount"], "due_date": r["due_date"],
                "overdue": r["due_date"] < today.isoformat()}
        if r["recurrence"]:
            item["repeat"] = r["recurrence"]
        items.append(item)
    return {"days": days, "total": round(sum(i["amount"] for i in items), 2), "bills": items}


# Members and chores ------------------------------------------------------

def _person(name: str) -> str:
    person = " ".join(name.split()).title()
    if not person:
        raise ValueError("person is empty")
    return person


def add_member(household: str, person: str) -> dict:
    person = _person(person)
    with connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO members (household, person) VALUES (?, ?)", (household, person))
    return {"status": "added" if cur.rowcount else "already_member", "person": person}


def remove_member(household: str, person: str) -> dict:
    """Take someone out of the chore rotation (their chore history is kept)."""
    person = _person(person)
    with connect() as c:
        cur = c.execute("DELETE FROM members WHERE household=? AND person=?", (household, person))
    return {"status": "removed" if cur.rowcount else "not_found", "person": person}


def list_members(household: str) -> dict:
    with connect() as c:
        members = [r["person"] for r in c.execute("SELECT person FROM members WHERE household=? ORDER BY person", (household,))]
    return {"count": len(members), "members": members}


def log_chore(household: str, chore: str, person: str, today: date | None = None, when: str = "today") -> dict:
    chore = chore.strip().lower()
    if not chore:
        raise ValueError("chore is empty")
    done_on = parse_date(when or "today", today)
    person = add_member(household, person)["person"]
    with connect() as c:
        c.execute(
            "INSERT INTO chores (household, chore, person, done_on) VALUES (?,?,?,?)",
            (household, chore, person, done_on),
        )
    return {"status": "logged", "chore": chore, "person": person, "date": done_on}


def whose_turn(household: str, chore: str) -> dict:
    """Fair rotation: the member who did this chore least recently (or never) is next."""
    chore = chore.strip().lower()
    with connect() as c:
        members = [r["person"] for r in c.execute("SELECT person FROM members WHERE household=? ORDER BY person", (household,))]
        last = {
            r["person"]: ((r["last_on"], r["last_id"]), r["n"])
            for r in c.execute(
                "SELECT person, max(done_on) AS last_on, max(id) AS last_id, count(*) AS n FROM chores "
                "WHERE household=? AND chore=? GROUP BY person",
                (household, chore),
            )
        }
    if not members:
        return {"status": "no_members", "message": "Tell me who lives here first, for example: log that Anna took out the trash."}
    nxt = min(members, key=lambda p: (p in last, last.get(p, (("", 0), 0))[0]))
    return {"chore": chore, "next": nxt, "history": {p: last.get(p, (None, 0))[1] for p in members}}


def chore_overview(household: str) -> list[dict]:
    """Every chore ever logged: who did it last, when, and whose turn it is now."""
    with connect() as c:
        rows = c.execute(
            "SELECT chore, person, done_on FROM chores WHERE household=? ORDER BY done_on DESC, id DESC", (household,)
        ).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r["chore"], {"chore": r["chore"], "last_person": r["person"], "last_done": r["done_on"]})
    return [{**info, "next": whose_turn(household, name).get("next")} for name, info in sorted(latest.items())]


# Pantry ------------------------------------------------------------------

def add_pantry(household: str, item: str, expires: str, today: date | None = None) -> dict:
    item = item.strip()
    if not item:
        raise ValueError("item is empty")
    exp = parse_date(expires, today)
    with connect() as c:
        c.execute("INSERT INTO pantry (household, item, expires_on) VALUES (?,?,?)", (household, item, exp))
    return {"status": "added", "item": item, "expires_on": exp}


def pantry_items(household: str, today: date | None = None) -> list[dict]:
    """Everything still in the pantry or fridge, soonest expiry first, with days left (negative once expired)."""
    today = today or date.today()
    with connect() as c:
        rows = c.execute(
            "SELECT id, item, expires_on FROM pantry WHERE household=? AND used=0 ORDER BY expires_on, id", (household,)
        ).fetchall()
    out = []
    for r in rows:
        left = (date.fromisoformat(r["expires_on"]) - today).days
        out.append({"id": r["id"], "item": r["item"], "expires_on": r["expires_on"], "days_left": left, "expired": left < 0})
    return out


def pantry_expiring(household: str, days: int = 3, today: date | None = None) -> dict:
    return {"days": days, "items": [i for i in pantry_items(household, today) if i["days_left"] <= days]}


def use_pantry(household: str, item: str = "", item_id: int | None = None) -> dict:
    """Mark one pantry item used: by id, or the soonest-expiring one with that name."""
    with connect() as c:
        if item_id is not None:
            row = c.execute("SELECT id, item FROM pantry WHERE household=? AND id=? AND used=0", (household, item_id)).fetchone()
        else:
            row = c.execute(
                "SELECT id, item FROM pantry WHERE household=? AND lower(item)=lower(?) AND used=0 ORDER BY expires_on LIMIT 1",
                (household, item.strip()),
            ).fetchone()
        if not row:
            return {"status": "not_found", "item": item}
        c.execute("UPDATE pantry SET used=1 WHERE id=?", (row["id"],))
    return {"status": "used", "item": row["item"]}


# Briefing ----------------------------------------------------------------

def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def weekly_briefing(household: str, today: date | None = None) -> dict:
    bills = upcoming_bills(household, 7, today)
    pantry = pantry_expiring(household, 3, today)
    groceries = grocery_list(household)
    parts = []
    if bills["bills"]:
        overdue = [b for b in bills["bills"] if b["overdue"]]
        parts.append(
            f"{_count(len(bills['bills']), 'bill')} due this week for {bills['total']:.2f} euro"
            + (f", {len(overdue)} already overdue ({', '.join(b['provider'] for b in overdue)})" if overdue else "")
        )
    if pantry["items"]:
        parts.append("use soon: " + ", ".join(i["item"] for i in pantry["items"]))
    if groceries["count"]:
        parts.append(f"{_count(groceries['count'], 'item')} on the shopping list")
    speech = ("This week: " + "; ".join(parts) + ".") if parts else "Nothing needs your attention this week."
    return {"speech": speech, "bills": bills, "pantry": pantry, "grocery_count": groceries["count"]}
