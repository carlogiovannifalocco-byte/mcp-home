"""mcp-home: a self-hosted MCP server (Streamable HTTP) that lets Alexa+ run a household's shared logistics."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import api, logic, oauth, speech
from .nl import plan_from_sentence

# RENDER_EXTERNAL_URL is set by Render itself, so a Blueprint deploy works without knowing its URL in advance.
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")
RESOURCE_URL = f"{PUBLIC_URL}/mcp"
auth_server = oauth.AuthServer(issuer=PUBLIC_URL, resource=RESOURCE_URL)


class HouseholdTokenVerifier:
    """Accepts the static HOUSEHOLD_TOKENS (MCP Inspector, the dashboard) and OAuth access tokens issued by
    this server's own authorization server (Alexa+ account linking, other MCP clients)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        household = oauth.household_for_token(token)
        if household is not None:
            return AccessToken(token=token, client_id=household, scopes=[oauth.USER_SCOPE], resource=RESOURCE_URL,
                               subject=household)
        return oauth.load_access_token(token, RESOURCE_URL)


def household() -> str:
    tok = get_access_token()
    if tok is None or oauth.USER_SCOPE not in tok.scopes:
        # ToolError reaches the client as a readable isError result instead of a generic failure.
        raise ToolError("This needs a linked household account: link mcp-home in the Alexa app first.")
    return tok.client_id


def transport_security(public_url: str = PUBLIC_URL) -> TransportSecuritySettings:
    """DNS-rebinding protection that accepts localhost and the public host the server is deployed under.

    Without this the SDK only allows localhost Host headers, so a deployed /mcp would answer 421.
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    parts = urlsplit(public_url)
    if parts.hostname and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        hosts += [parts.netloc, f"{parts.hostname}:*"]
        origins.append(f"{parts.scheme}://{parts.netloc}")
    hosts += [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


mcp = MCPServer(
    name="mcp-home",
    title="Home",
    instructions=(
        "Home keeps one household's shared shopping list, bills, chores and pantry. "
        "Every result has a `speech` field: a short sentence in the household's language, without ids or tool "
        "names, that you can read out as is. Paying a bill, removing a member and free-form sentences answer "
        "status 'needs_confirmation' first: read the question back and call again with confirm=true only after an "
        "explicit yes. Amounts are in euro. Dates can be ISO or words like 'tomorrow' or 'in 3 days'."
    ),
    version="0.4.0",
    token_verifier=HouseholdTokenVerifier(),
    # No required_scopes: a client_credentials `mcp:service` token may initialize and list tools, and every tool
    # checks the household scope itself through household(). Tokens must be bound to this resource (RFC 8707).
    auth=AuthSettings(issuer_url=PUBLIC_URL, resource_server_url=RESOURCE_URL, validate_token_resource=True),
)


def _tool(title: str, *, read_only: bool = False, destructive: bool = False, idempotent: bool = False,
          open_world: bool = False):
    """Register a tool with explicit MCP annotations. The spec defaults destructiveHint to true for any tool that
    is not read only, so additive tools say destructiveHint=false and only the two that pay or remove say true."""
    if read_only:
        ann = ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=open_world)
    else:
        ann = ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=destructive, idempotentHint=idempotent,
                              openWorldHint=open_world)
    return mcp.tool(title=title, annotations=ann)


def spoken(tool: str, result: dict, hh: str) -> dict:
    """Add the sentence a voice client can read as is: no ids, no tool names, the household's language."""
    return {**result, "speech": speech.say(tool, result, logic.get_language(hh))}


def needs_confirmation(hh: str, tool: str, key: str, payload: dict, question: str, details: dict) -> dict:
    """Read the action back and wait for a yes. Only a later call with confirm=true can run it (see logic)."""
    logic.ask_confirmation(hh, tool, key, payload)
    return {"status": "needs_confirmation", "question": question, "speech": question, "details": details,
            "how_to_confirm": "Ask the person. Only if they say yes, call this tool again with the same arguments "
                              "and confirm=true. If they say no, do nothing."}


@_tool("Add to the shopping list", idempotent=True)
def add_grocery_item(item: str, quantity: str = "", added_by: str = "") -> dict:
    """Add an item to the shared shopping list. It is grouped by supermarket aisle automatically; duplicates are
    ignored."""
    hh = household()
    return spoken("add_grocery_item", logic.add_grocery(hh, item, quantity, added_by), hh)


@_tool("Read the shopping list", read_only=True)
def get_grocery_list() -> dict:
    """Read the shopping list grouped by aisle, in the order you walk a supermarket."""
    hh = household()
    return spoken("get_grocery_list", logic.grocery_list(hh), hh)


@_tool("Check off a shopping list item")
def check_off_grocery_item(item: str) -> dict:
    """Mark a shopping list item as bought."""
    hh = household()
    return spoken("check_off_grocery_item", logic.check_off(hh, item), hh)


@_tool("Record a bill")
def add_bill(provider: str, amount: float, due: str, repeat: str = "") -> dict:
    """Record a bill to pay. `due` is YYYY-MM-DD or words like 'tomorrow' or 'in 10 days'.
    `repeat` is optional: weekly, monthly, bimonthly, quarterly or yearly; paying it schedules the next one."""
    hh = household()
    return spoken("add_bill", logic.add_bill(hh, provider, amount, due, repeat=repeat), hh)


@_tool("Mark a bill as paid", destructive=True)
def mark_bill_paid(provider: str, confirm: bool = False) -> dict:
    """Mark the oldest unpaid bill from this provider as paid (recurring bills get their next due date).
    A payment is high-consequence: the first call only reads the bill back and returns status
    "needs_confirmation". Ask the person; only if they say yes, call again with confirm=true."""
    hh = household()
    lang = logic.get_language(hh)
    bill = logic.open_bill(hh, provider)
    if bill is None:
        return spoken("mark_bill_paid", {"status": "not_found", "provider": provider}, hh)
    pending = logic.take_confirmation(hh, "mark_bill_paid", provider) if confirm else None
    if pending is None:
        when = speech.say_date(bill["due_date"], lang)
        q = (f"Segno come pagata la bolletta {bill['provider']} di {speech.euro(bill['amount'])}, in scadenza {when}?"
             if lang == "it" else
             f"Mark the {bill['provider']} bill of {speech.euro(bill['amount'])}, due {when}, as paid?")
        return needs_confirmation(hh, "mark_bill_paid", provider, {"bill_id": bill["id"]}, q,
                                  {"provider": bill["provider"], "amount": bill["amount"], "due_date": bill["due_date"]})
    return spoken("mark_bill_paid", logic.pay_bill(hh, bill_id=pending["bill_id"]), hh)


@_tool("Bills due soon", read_only=True)
def upcoming_deadlines(days: int = 14) -> dict:
    """Unpaid bills due within `days`, including overdue ones, with the total."""
    hh = household()
    return spoken("upcoming_deadlines", logic.upcoming_bills(hh, days), hh)


@_tool("Log a chore")
def log_chore(chore: str, person: str, when: str = "today") -> dict:
    """Record that someone did a chore (for example 'trash', 'dishes', 'laundry').
    `when` defaults to today; 'yesterday', '2 days ago' or YYYY-MM-DD also work."""
    hh = household()
    return spoken("log_chore", logic.log_chore(hh, chore, person, when=when), hh)


@_tool("Whose turn is it", read_only=True)
def whose_turn(chore: str) -> dict:
    """Say who should do a chore next, rotating fairly between household members."""
    hh = household()
    return spoken("whose_turn", logic.whose_turn(hh, chore), hh)


@_tool("Track food with an expiry date")
def add_pantry_item(item: str, expires: str) -> dict:
    """Track a food item in the pantry or fridge with its expiry date."""
    hh = household()
    return spoken("add_pantry_item", logic.add_pantry(hh, item, expires), hh)


@_tool("Who lives here", read_only=True)
def household_members() -> dict:
    """Who lives here, i.e. who takes part in the chore rotation."""
    hh = household()
    return spoken("household_members", logic.list_members(hh), hh)


@_tool("Add a household member", idempotent=True)
def add_household_member(person: str) -> dict:
    """Add someone to the household so they join the chore rotation."""
    hh = household()
    return spoken("add_household_member", logic.add_member(hh, person), hh)


@_tool("Remove a household member", destructive=True)
def remove_household_member(person: str, confirm: bool = False) -> dict:
    """Take someone out of the chore rotation (for example after they move out). Their history is kept.
    The first call only reads it back and returns status "needs_confirmation"; call again with confirm=true
    only after the person says yes."""
    hh = household()
    lang = logic.get_language(hh)
    if not logic.is_member(hh, person):
        return spoken("remove_household_member", {"status": "not_found", "person": person.strip().title()}, hh)
    if not (confirm and logic.take_confirmation(hh, "remove_household_member", person) is not None):
        name = " ".join(person.split()).title()
        q = (f"Tolgo {name} dalla famiglia e dai turni? La sua storia resta salvata." if lang == "it" else
             f"Remove {name} from the household and the chore rotation? Their history is kept.")
        return needs_confirmation(hh, "remove_household_member", person, {}, q, {"person": name})
    return spoken("remove_household_member", logic.remove_member(hh, person), hh)


@_tool("Food to eat soon", read_only=True)
def pantry_expiring(days: int = 3) -> dict:
    """Food that expires within `days` (or already has), so it gets eaten instead of thrown away."""
    hh = household()
    return spoken("pantry_expiring", logic.pantry_expiring(hh, days), hh)


@_tool("Mark food as used")
def mark_pantry_used(item: str) -> dict:
    """Mark a pantry item as used up."""
    hh = household()
    return spoken("mark_pantry_used", logic.use_pantry(hh, item), hh)


@_tool("Weekly briefing", read_only=True)
def weekly_briefing() -> dict:
    """One spoken summary of what needs attention this week: bills, food to use, shopping."""
    return logic.weekly_briefing(household())


@_tool("What can you do", read_only=True)
def what_can_you_do() -> dict:
    """Answer "what can you do?" or "help": a short spoken summary of what this add-on does, with examples.
    For a household nobody has set up yet it also says how to start."""
    hh = household()
    lang, empty = logic.get_language(hh), logic.is_empty(hh)
    return {"speech": speech.capabilities(lang, empty), "household_set_up": not empty, "language": lang}


@_tool("Set the spoken language", idempotent=True)
def set_language(language: str) -> dict:
    """Choose the language of the spoken replies for this household: English or Italian ('en', 'it')."""
    hh = household()
    return spoken("set_language", logic.set_language(hh, language), hh)


ACTION_RUNNERS = {
    "add_grocery_item": lambda hh, a: logic.add_grocery(hh, a["item"], a.get("quantity", "")),
    "add_bill": lambda hh, a: logic.add_bill(hh, a["provider"], float(a["amount"]), a["due"], repeat=a.get("repeat", "")),
    "log_chore": lambda hh, a: logic.log_chore(hh, a["chore"], a["person"]),
    "add_pantry_item": lambda hh, a: logic.add_pantry(hh, a["item"], a["expires"]),
}


@_tool("Do everything in a sentence", open_world=True)
def do_from_sentence(sentence: str, confirm: bool = False) -> dict:
    """Turn a free-form sentence ('we're out of milk and the gas bill of 80 euro is due Friday') into actions.
    Uses Amazon Bedrock when configured. The first call plans the actions and reads them back (status
    "needs_confirmation"); call again with the same sentence and confirm=true after the person says yes. The
    confirmed call runs exactly the plan that was read back, without asking the model again."""
    hh = household()
    lang = logic.get_language(hh)
    actions = logic.take_confirmation(hh, "do_from_sentence", sentence) if confirm else None
    if actions is None:
        try:
            planned = plan_from_sentence(sentence)
        except ValueError as exc:  # e.g. Bedrock not configured: say so instead of a generic tool failure
            raise ToolError(str(exc)) from exc
        # Keep only actions the tools can run and that can be read back in full (no missing amount or date).
        planned = [a for a in planned if a.get("tool") in ACTION_RUNNERS and isinstance(a.get("args"), dict)
                   and speech.describe_action(a, lang)]
        said = [speech.describe_action(a, lang) for a in planned]
        if not planned:
            return spoken("do_from_sentence", {"actions": []}, hh)
        q = (f"Vuoi che io: {speech.join(said, lang)}?" if lang == "it" else
             f"I'll {speech.join(said)}. Shall I go ahead?")
        return needs_confirmation(hh, "do_from_sentence", sentence, {"actions": planned}, q, {"actions": planned})
    results = []
    for action in actions["actions"]:
        try:
            out = ACTION_RUNNERS[action["tool"]](hh, action["args"])
            results.append({**out, "speech": speech.say(action["tool"], out, lang)})
        except (KeyError, ValueError) as exc:
            results.append({"status": "error", "action": action, "error": str(exc)})
    return spoken("do_from_sentence", {"actions": results}, hh)


@mcp.resource("home://briefing", mime_type="application/json")
def briefing_resource() -> dict:
    """What needs attention this week."""
    return logic.weekly_briefing(household())


@mcp.prompt(title="Set up the household")
def set_up_household() -> str:
    """Guided first run: language, who lives here, recurring bills, chores."""
    return (
        "Help me set up Home for our household, one short question at a time, and use the Home tools as we go:\n"
        "1. Ask whether we want replies in English or Italian, then call set_language.\n"
        "2. Ask who lives here and call add_household_member for each person.\n"
        "3. Ask about regular bills (electricity, gas, internet, rent): provider, amount, next due date and how often. "
        "Call add_bill with repeat for each.\n"
        "4. Ask which chores we share (trash, dishes, laundry) and who did each one last; call log_chore.\n"
        "5. Finish with weekly_briefing and read its speech."
    )


@mcp.prompt(title="Weekly review")
def weekly_review() -> str:
    """Sunday-evening review: what is due, what to eat first, whose turn it is."""
    return (
        "Give us our weekly home review. Call weekly_briefing, upcoming_deadlines with days=7 and pantry_expiring, "
        "then whose_turn for each chore we share. Answer in at most four short spoken sentences, without ids or "
        "tool names. Do not change anything."
    )


class Without401Challenge:
    """Drop WWW-Authenticate from 401s on /mcp when ALEXA_COMPAT=1 (or WWW_AUTHENTICATE=off).

    The Alexa+ MCP quick start describes the server answering 401 "without a WWW-Authenticate header" and finding
    the metadata at the RFC 9728 well-known URI instead. The MCP spec allows either, so the header stays on by
    default (MCP Inspector and other clients use it) and this switch matches Amazon's description exactly.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            return await self.app(scope, receive, send)

        async def strip(message):
            if message["type"] == "http.response.start" and message["status"] == 401:
                message = {**message, "headers": [(k, v) for k, v in message.get("headers", [])
                                                  if k.lower() != b"www-authenticate"]}
            await send(message)

        await self.app(scope, receive, strip)


api.register(mcp)
app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=transport_security())
# The OAuth routes go first so our protected-resource metadata (with scopes_supported) wins over the SDK's default.
app.router.routes[0:0] = auth_server.routes()
if (os.getenv("WWW_AUTHENTICATE", "on").strip().lower() in ("off", "0", "false", "no")
        or os.getenv("ALEXA_COMPAT", "").strip().lower() in ("1", "true", "yes", "on")):
    app = Without401Challenge(app)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
