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

from . import api, logic, oauth
from .nl import plan_from_sentence

READ_ONLY = ToolAnnotations(readOnlyHint=True)

PUBLIC_URL = (os.getenv("PUBLIC_URL") or "http://localhost:8000").rstrip("/")
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
        "Prefer short spoken answers. Amounts are in euro. Dates can be ISO or words like 'tomorrow' or 'in 3 days'."
    ),
    version="0.3.0",
    token_verifier=HouseholdTokenVerifier(),
    # No required_scopes: a client_credentials `mcp:service` token may initialize and list tools, and every tool
    # checks the household scope itself through household(). Tokens must be bound to this resource (RFC 8707).
    auth=AuthSettings(issuer_url=PUBLIC_URL, resource_server_url=RESOURCE_URL, validate_token_resource=True),
)


@mcp.tool()
def add_grocery_item(item: str, quantity: str = "", added_by: str = "") -> dict:
    """Add an item to the shared shopping list. It is grouped by supermarket aisle automatically."""
    return logic.add_grocery(household(), item, quantity, added_by)


@mcp.tool(annotations=READ_ONLY)
def get_grocery_list() -> dict:
    """Read the shopping list grouped by aisle, in the order you walk a supermarket."""
    return logic.grocery_list(household())


@mcp.tool()
def check_off_grocery_item(item: str) -> dict:
    """Mark a shopping list item as bought."""
    return logic.check_off(household(), item)


@mcp.tool()
def add_bill(provider: str, amount: float, due: str, repeat: str = "") -> dict:
    """Record a bill to pay. `due` is YYYY-MM-DD or words like 'tomorrow' or 'in 10 days'.
    `repeat` is optional: weekly, monthly, bimonthly, quarterly or yearly; paying it schedules the next one."""
    return logic.add_bill(household(), provider, amount, due, repeat=repeat)


@mcp.tool()
def mark_bill_paid(provider: str) -> dict:
    """Mark the oldest unpaid bill from this provider as paid (recurring bills get their next due date)."""
    return logic.pay_bill(household(), provider)


@mcp.tool(annotations=READ_ONLY)
def upcoming_deadlines(days: int = 14) -> dict:
    """Unpaid bills due within `days`, including overdue ones, with the total."""
    return logic.upcoming_bills(household(), days)


@mcp.tool()
def log_chore(chore: str, person: str, when: str = "today") -> dict:
    """Record that someone did a chore (for example 'trash', 'dishes', 'laundry').
    `when` defaults to today; 'yesterday', '2 days ago' or YYYY-MM-DD also work."""
    return logic.log_chore(household(), chore, person, when=when)


@mcp.tool(annotations=READ_ONLY)
def whose_turn(chore: str) -> dict:
    """Say who should do a chore next, rotating fairly between household members."""
    return logic.whose_turn(household(), chore)


@mcp.tool()
def add_pantry_item(item: str, expires: str) -> dict:
    """Track a food item in the pantry or fridge with its expiry date."""
    return logic.add_pantry(household(), item, expires)


@mcp.tool(annotations=READ_ONLY)
def household_members() -> dict:
    """Who lives here, i.e. who takes part in the chore rotation."""
    return logic.list_members(household())


@mcp.tool()
def add_household_member(person: str) -> dict:
    """Add someone to the household so they join the chore rotation."""
    return logic.add_member(household(), person)


@mcp.tool()
def remove_household_member(person: str) -> dict:
    """Take someone out of the chore rotation (for example after they move out). Their history is kept."""
    return logic.remove_member(household(), person)


@mcp.tool(annotations=READ_ONLY)
def pantry_expiring(days: int = 3) -> dict:
    """Food that expires within `days` (or already has), so it gets eaten instead of thrown away."""
    return logic.pantry_expiring(household(), days)


@mcp.tool()
def mark_pantry_used(item: str) -> dict:
    """Mark a pantry item as used up."""
    return logic.use_pantry(household(), item)


@mcp.tool(annotations=READ_ONLY)
def weekly_briefing() -> dict:
    """One spoken summary of what needs attention this week: bills, food to use, shopping."""
    return logic.weekly_briefing(household())


@mcp.tool()
def do_from_sentence(sentence: str) -> dict:
    """Turn a free-form sentence ('we're out of milk and the gas bill of 80 euro is due Friday') into actions
    and run them. Uses Amazon Bedrock when configured."""
    hh = household()
    try:
        actions = plan_from_sentence(sentence)
    except ValueError as exc:  # e.g. Bedrock not configured: say so instead of a generic tool failure
        raise ToolError(str(exc)) from exc
    runners = {
        "add_grocery_item": lambda a: logic.add_grocery(hh, a["item"], a.get("quantity", "")),
        "add_bill": lambda a: logic.add_bill(hh, a["provider"], float(a["amount"]), a["due"], repeat=a.get("repeat", "")),
        "log_chore": lambda a: logic.log_chore(hh, a["chore"], a["person"]),
        "add_pantry_item": lambda a: logic.add_pantry(hh, a["item"], a["expires"]),
    }
    results = []
    for action in actions:
        run = runners.get(action.get("tool"))
        try:
            results.append(run(action["args"]) if run else {"status": "skipped", "action": action})
        except (KeyError, ValueError) as exc:
            results.append({"status": "error", "action": action, "error": str(exc)})
    return {"actions": results}


@mcp.resource("home://briefing", mime_type="application/json")
def briefing_resource() -> dict:
    """What needs attention this week."""
    return logic.weekly_briefing(household())


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
