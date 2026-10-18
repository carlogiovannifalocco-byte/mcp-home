"""What Alexa says back: one short sentence per tool result, in English or Italian.

Amazon's Alexa+ functional requirements ask that customer-facing responses "surface no API codes, tool names, JSON,
or internal IDs", that every error has an "actionable next step", and that high-consequence actions are read back
with their key details before an explicit "yes". Every tool result carries a `speech` field written to those rules,
so a voice client can read it as is (and the assistant can still paraphrase it).
"""

from __future__ import annotations

from datetime import date

LANGUAGES = ("en", "it")

MONTHS_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
             "ottobre", "novembre", "dicembre"]
DAYS_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
AISLES_IT = {"produce": "frutta e verdura", "dairy": "latticini", "bakery": "panetteria", "meat & fish": "carne e pesce",
             "pantry": "dispensa", "frozen": "surgelati", "drinks": "bevande", "household": "casa", "other": "varie"}


def normalize_language(value: str) -> str:
    v = (value or "").strip().lower()
    aliases = {"en": "en", "english": "en", "inglese": "en", "it": "it", "italian": "it", "italiano": "it"}
    if v not in aliases:
        raise ValueError("Say English or Italian.")
    return aliases[v]


def say_date(iso: str, lang: str = "en", today: date | None = None) -> str:
    d, today = date.fromisoformat(iso), today or date.today()
    diff = (d - today).days
    rel = {"en": {0: "today", 1: "tomorrow", -1: "yesterday"}, "it": {0: "oggi", 1: "domani", -1: "ieri"}}[lang]
    if diff in rel:
        return rel[diff]
    if lang == "it":
        return f"{DAYS_IT[d.weekday()]} {d.day} {MONTHS_IT[d.month - 1]}"
    return f"{d:%A} {d.day} {d:%B}"


def euro(amount: float) -> str:
    text = f"{amount:.2f}"
    return (text[:-3] if text.endswith(".00") else text) + " euro"


def join(items: list[str], lang: str = "en") -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + (" e " if lang == "it" else " and ") + items[-1]


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def briefing(bills: dict, pantry: dict, grocery_count: int, lang: str = "en") -> str:
    """The weekly summary, read aloud. The English wording is the one the dashboard has always shown."""
    parts = []
    if bills["bills"]:
        overdue = [b["provider"] for b in bills["bills"] if b["overdue"]]
        n = len(bills["bills"])
        if lang == "it":
            parts.append(f"{_n(n, 'bolletta', 'bollette')} in scadenza per {bills['total']:.2f} euro"
                         + (f", {len(overdue)} già scadut{'a' if len(overdue) == 1 else 'e'} ({', '.join(overdue)})"
                            if overdue else ""))
        else:
            parts.append(f"{_n(n, 'bill', 'bills')} due this week for {bills['total']:.2f} euro"
                         + (f", {len(overdue)} already overdue ({', '.join(overdue)})" if overdue else ""))
    if pantry["items"]:
        names = ", ".join(i["item"] for i in pantry["items"])
        parts.append(("da consumare presto: " if lang == "it" else "use soon: ") + names)
    if grocery_count:
        parts.append(f"{_n(grocery_count, 'cosa', 'cose')} sulla lista della spesa" if lang == "it"
                     else f"{_n(grocery_count, 'item', 'items')} on the shopping list")
    if not parts:
        return "Questa settimana non c'è niente da sistemare." if lang == "it" else "Nothing needs your attention this week."
    return ("Questa settimana: " if lang == "it" else "This week: ") + "; ".join(parts) + "."


CHORES_EN = {"trash": "take out the trash", "rubbish": "take out the rubbish", "recycling": "take out the recycling",
             "dog": "walk the dog", "plants": "water the plants", "bathroom": "clean the bathroom",
             "dishes": "do the dishes", "laundry": "do the laundry"}
CHORES_IT = {"trash": "portare fuori la spazzatura", "spazzatura": "portare fuori la spazzatura",
             "dishes": "lavare i piatti", "piatti": "lavare i piatti", "laundry": "fare il bucato",
             "bucato": "fare il bucato", "dog": "portare fuori il cane", "cane": "portare fuori il cane",
             "bathroom": "pulire il bagno", "bagno": "pulire il bagno", "plants": "innaffiare le piante"}


def _chore(chore: str, lang: str) -> str:
    if lang == "it":
        return CHORES_IT.get(chore, f"occuparsi di: {chore}")
    return CHORES_EN.get(chore, f"do the {chore}")


def say(tool: str, r: dict, lang: str = "en", today: date | None = None) -> str:
    """One sentence for a tool result. Never mentions ids, tool names or JSON."""
    it = lang == "it"
    st = r.get("status")
    d = lambda iso: say_date(iso, lang, today)  # noqa: E731
    if st == "needs_confirmation":
        return r["question"]
    if st == "cancelled":
        return "Va bene, non faccio niente." if it else "OK, I won't do that."
    if tool == "add_grocery_item":
        if st == "already_on_list":
            return f"{r['item']} è già sulla lista." if it else f"{r['item']} is already on the shopping list."
        if it:
            return f"Ho aggiunto {r['item']} alla lista, reparto {AISLES_IT.get(r['aisle'], r['aisle'])}."
        return f"Added {r['item']} to the shopping list, in the {r['aisle']} aisle."
    if tool == "get_grocery_list":
        items = [i for group in r["by_aisle"].values() for i in group]
        if not items:
            return "La lista della spesa è vuota." if it else "The shopping list is empty."
        if it:
            return f"Sulla lista ci {'è' if len(items) == 1 else 'sono'} {_n(len(items), 'cosa', 'cose')}: {join(items, lang)}."
        return f"There {'is' if len(items) == 1 else 'are'} {_n(len(items), 'thing', 'things')} on the list: {join(items)}."
    if tool == "check_off_grocery_item":
        if st == "checked_off":
            return f"Ho spuntato {r['item']}." if it else f"Checked {r['item']} off the list."
        return (f"Non trovo {r['item']} sulla lista. Vuoi che ti legga la lista?" if it
                else f"I couldn't find {r['item']} on the list. Want me to read the list?")
    if tool == "add_bill":
        rep = r.get("repeat")
        if it:
            return f"Segnato: bolletta {r['provider']}, {euro(r['amount'])}, scade {d(r['due_date'])}" + (
                f", si ripete ({rep})." if rep else ".")
        return f"Got it: the {r['provider']} bill, {euro(r['amount'])}, due {d(r['due_date'])}" + (
            f", repeating {rep}." if rep else ".")
    if tool == "mark_bill_paid":
        if st != "paid":
            return (f"Non trovo bollette aperte di {r['provider']}. Vuoi sentire le scadenze?" if it
                    else f"I couldn't find an open {r['provider']} bill. Want to hear what's due?")
        nxt = r.get("next_due")
        if it:
            return f"Bolletta {r['provider']} di {euro(r['amount'])} segnata come pagata." + (
                f" La prossima scade {d(nxt)}." if nxt else "")
        return f"Marked the {r['provider']} bill of {euro(r['amount'])} as paid." + (
            f" The next one is due {d(nxt)}." if nxt else "")
    if tool == "upcoming_deadlines":
        bills = r["bills"]
        if not bills:
            return (f"Nessuna bolletta nei prossimi {r['days']} giorni." if it
                    else f"No bills are due in the next {r['days']} days.")
        if it:
            parts = [f"{b['provider']}, {euro(b['amount'])}, " + ("scaduta " if b["overdue"] else "scade ") + d(b["due_date"])
                     for b in bills]
            return f"{_n(len(bills), 'bolletta', 'bollette')} per {euro(r['total'])}: {join(parts, lang)}."
        parts = [f"{b['provider']}, {euro(b['amount'])}, " + ("overdue since " if b["overdue"] else "due ") + d(b["due_date"])
                 for b in bills]
        return f"{_n(len(bills), 'bill', 'bills')} for {euro(r['total'])}: {join(parts)}."
    if tool == "log_chore":
        return (f"Segnato: {r['person']}, {r['chore']}, {d(r['date'])}." if it
                else f"Noted: {r['person']} did the {r['chore']} {d(r['date'])}.")
    if tool == "whose_turn":
        if st == "no_members":
            return ("Dimmi prima chi abita qui, per esempio: aggiungi Anna alla famiglia." if it
                    else "Tell me who lives here first, for example: add Anna to the household.")
        return (f"Tocca a {r['next']} {_chore(r['chore'], lang)}." if it
                else f"It's {r['next']}'s turn to {_chore(r['chore'], lang)}.")
    if tool == "household_members":
        if not r["members"]:
            return ("Non c'è ancora nessuno in famiglia. Dimmi chi abita qui." if it
                    else "Nobody is in the household yet. Tell me who lives here.")
        n = len(r["members"])
        return (f"{join(r['members'], lang)} {'abita' if n == 1 else 'abitano'} qui." if it
                else f"{join(r['members'])} {'lives' if n == 1 else 'live'} here.")
    if tool == "add_household_member":
        if st == "added":
            return (f"Benvenuto, {r['person']}: ora fa parte dei turni." if it
                    else f"Welcome, {r['person']}. {r['person']} is now in the chore rotation.")
        return f"{r['person']} è già in famiglia." if it else f"{r['person']} is already in the household."
    if tool == "remove_household_member":
        if st == "removed":
            return f"{r['person']} non è più nei turni." if it else f"{r['person']} is out of the chore rotation."
        return f"{r['person']} non era in famiglia." if it else f"{r['person']} wasn't in the household."
    if tool == "add_pantry_item":
        return (f"Tengo d'occhio {r['item']}: scade {d(r['expires_on'])}." if it
                else f"Tracking {r['item']}, it expires {d(r['expires_on'])}.")
    if tool == "mark_pantry_used":
        if st == "used":
            return f"Ho segnato {r['item']} come finito." if it else f"Marked {r['item']} as used."
        return f"Non trovo {r['item']} in dispensa." if it else f"I couldn't find {r['item']} in the pantry."
    if tool == "pantry_expiring":
        items = r["items"]
        if not items:
            return "Non scade niente a breve." if it else "Nothing is about to expire."
        if it:
            return "Da consumare presto: " + join([i["item"] + (" (già scaduto)" if i["expired"] else ", " + d(i["expires_on"]))
                                                    for i in items], lang) + "."
        return "Use soon: " + join([i["item"] + (" (already expired)" if i["expired"] else ", " + d(i["expires_on"]))
                                    for i in items]) + "."
    if tool == "set_language":
        return "D'accordo, da ora ti rispondo in italiano." if r["language"] == "it" else "OK, I'll answer in English from now on."
    if tool == "do_from_sentence":
        said = [s for s in (a.get("speech", "") for a in r.get("actions", [])) if s]
        if not said:
            return ("Non ho trovato niente da fare in questa frase." if it
                    else "I didn't find anything to do in that sentence.")
        return " ".join(said)
    return r.get("speech", "")


def describe_action(action: dict, lang: str = "en", today: date | None = None) -> str:
    """A planned action, read back before it runs ("add milk to the shopping list")."""
    a, tool, it = action.get("args", {}), action.get("tool"), lang == "it"
    try:
        if tool == "add_grocery_item":
            return f"aggiungere {a['item']} alla lista" if it else f"add {a['item']} to the shopping list"
        if tool == "add_bill":
            due = say_date(a["due"], lang, today) if len(str(a["due"])) == 10 else a["due"]
            return (f"segnare la bolletta {a['provider']} di {euro(float(a['amount']))} in scadenza {due}" if it
                    else f"record the {a['provider']} bill, {euro(float(a['amount']))}, due {due}")
        if tool == "log_chore":
            return f"segnare che {a['person']} ha fatto: {a['chore']}" if it else f"note that {a['person']} did the {a['chore']}"
        if tool == "add_pantry_item":
            exp = say_date(a["expires"], lang, today) if len(str(a["expires"])) == 10 else a["expires"]
            return f"tenere d'occhio {a['item']}, scade {exp}" if it else f"track {a['item']}, expiring {exp}"
    except (KeyError, ValueError, TypeError):
        pass
    return ""


def capabilities(lang: str = "en", empty: bool = False) -> str:
    """The answer to "What can you do?" (an Alexa+ functional requirement)."""
    if lang == "it":
        text = ("Tengo la memoria condivisa della casa: la lista della spesa divisa per reparto, le bollette e le "
                "scadenze, i turni delle faccende e il cibo che sta per scadere. Prova: aggiungi il latte alla lista, "
                "a chi tocca la spazzatura, o cosa c'è da fare questa settimana.")
        return text + (" Per iniziare, dimmi chi abita qui." if empty else "")
    text = ("I keep your home's shared memory: the shopping list by aisle, bills and due dates, whose turn it is for "
            "chores, and food that is about to expire. Try: add milk to the list, whose turn is it to take out the "
            "trash, or what do we need to deal with this week.")
    return text + (" To get started, tell me who lives here." if empty else "")
