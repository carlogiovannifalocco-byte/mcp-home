"""mcp-home: a self-hosted MCP server (Streamable HTTP) that lets Alexa+ run a household's shared logistics."""

from __future__ import annotations

import os
import secrets
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import api, logic
from .nl import plan_from_sentence

READ_ONLY = ToolAnnotations(readOnlyHint=True)


def _tokens() -> dict[str, str]:
    """HOUSEHOLD_TOKENS="token1:rossi,token2:bianchi" maps bearer tokens to households."""
    pairs = (p.split(":", 1) for p in os.getenv("HOUSEHOLD_TOKENS", "").split(",") if ":" in p)
    return {tok.strip(): hh.strip() for tok, hh in pairs}


class HouseholdTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        for known, household in _tokens().items():
            if secrets.compare_digest(known, token):
                return AccessToken(token=token, client_id=household, scopes=["household"])
        return None


def household() -> str:
    tok = get_access_token()
    if tok is None:
        raise PermissionError("missing household token")
    return tok.client_id


PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")


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
    version="0.2.0",
    token_verifier=HouseholdTokenVerifier(),
    auth=AuthSettings(issuer_url=PUBLIC_URL, resource_server_url=f"{PUBLIC_URL}/mcp", required_scopes=["household"], validate_token_resource=False),
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
    actions = plan_from_sentence(sentence)
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


api.register(mcp)
app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=transport_security())


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
