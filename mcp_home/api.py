"""Web dashboard and its small JSON API, served by the same app as the MCP endpoint.

The API sits behind the same bearer tokens as /mcp (the SDK's AuthenticationMiddleware already wraps every route),
and every handler calls the functions in `logic`, so the dashboard shows exactly what Alexa reads and writes.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import logic, oauth

STATIC = Path(__file__).parent / "static"
BILL_DAYS = 31  # the dashboard looks a month ahead; the spoken briefing keeps to one week

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _household(request: Request) -> str:
    user = request.user
    if not getattr(user, "is_authenticated", False) or "household" not in request.auth.scopes:
        raise ApiError("A valid household token is required (Authorization: Bearer <token>).", 401)
    return user.access_token.client_id


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError("Request body must be JSON.") from None
    if not isinstance(data, dict):
        raise ApiError("Request body must be a JSON object.")
    return data


def _text(data: dict[str, Any], key: str, required: bool = True, limit: int = 120) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ApiError(f"'{key}' must be text.")
    value = value.strip()
    if required and not value:
        raise ApiError(f"'{key}' is required.")
    if len(value) > limit:
        raise ApiError(f"'{key}' is too long (max {limit} characters).")
    return value


def _int(request: Request, key: str) -> int:
    try:
        return int(request.path_params[key])
    except (KeyError, ValueError):
        raise ApiError(f"Invalid {key}.") from None


def mcp_url(request: Request) -> str:
    base = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or str(request.base_url)
    return base.rstrip("/") + "/mcp"


def state(household: str, today: date | None = None) -> dict:
    """Everything the dashboard shows, built only from `logic` so it matches what the MCP tools return."""
    today = today or date.today()
    briefing = logic.weekly_briefing(household, today)
    return {
        "household": household,
        "today": today.isoformat(),
        "briefing": {"speech": briefing["speech"], "bills_due_this_week": len(briefing["bills"]["bills"]),
                     "bills_total_this_week": briefing["bills"]["total"],
                     "use_soon": [i["item"] for i in briefing["pantry"]["items"]],
                     "grocery_count": briefing["grocery_count"]},
        "groceries": logic.grocery_items(household),
        "aisles": logic.WALK_ORDER,
        "bills": logic.upcoming_bills(household, BILL_DAYS, today),
        "chores": logic.chore_overview(household),
        "members": logic.list_members(household)["members"],
        "pantry": logic.pantry_items(household, today),
    }


Handler = Callable[[Request], Awaitable[Response]]


def endpoint(action: Callable[[str, Request, dict[str, Any]], Any], *, needs_body: bool = True) -> Handler:
    """Authenticate, parse, run one logic call off the event loop, and return its result plus the fresh state."""

    async def handler(request: Request) -> Response:
        try:
            hh = _household(request)
            data = await _body(request) if needs_body else {}
            result = await run_in_threadpool(action, hh, request, data)
            payload = {"result": result, "state": await run_in_threadpool(state, hh)}
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        except ApiError as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
            return JSONResponse({"error": str(exc)}, status_code=exc.status, headers=headers)
        except ValueError as exc:  # validation errors from logic (dates, amounts, recurrences)
            return JSONResponse({"error": str(exc)}, status_code=400)

    return handler


def _amount(data: dict[str, Any]) -> float:
    raw = data.get("amount")
    if isinstance(raw, bool):
        raise ApiError("'amount' must be a number.")
    try:
        amount = float(str(raw).replace(",", "."))
    except ValueError:
        raise ApiError("'amount' must be a number.") from None
    if not 0 < amount < 1_000_000:
        raise ApiError("'amount' must be a positive number.")
    return amount


ACTIONS: dict[tuple[str, str], Callable[[str, Request, dict[str, Any]], Any]] = {
    ("POST", "/api/groceries"): lambda hh, r, d: logic.add_grocery(
        hh, _text(d, "item"), _text(d, "quantity", False, 40), _text(d, "added_by", False, 40)),
    ("POST", "/api/groceries/check"): lambda hh, r, d: logic.check_off(hh, _text(d, "item")),
    ("POST", "/api/bills"): lambda hh, r, d: logic.add_bill(
        hh, _text(d, "provider"), _amount(d), _text(d, "due", limit=40), repeat=_text(d, "repeat", False, 20)),
    ("POST", "/api/bills/{bill_id}/pay"): lambda hh, r, d: logic.pay_bill(hh, bill_id=_int(r, "bill_id")),
    ("POST", "/api/chores"): lambda hh, r, d: logic.log_chore(
        hh, _text(d, "chore", limit=60), _text(d, "person", limit=40), when=_text(d, "when", False, 40) or "today"),
    ("POST", "/api/pantry"): lambda hh, r, d: logic.add_pantry(hh, _text(d, "item"), _text(d, "expires", limit=40)),
    ("POST", "/api/pantry/{item_id}/use"): lambda hh, r, d: logic.use_pantry(hh, item_id=_int(r, "item_id")),
    ("POST", "/api/members"): lambda hh, r, d: logic.add_member(hh, _text(d, "person", limit=40)),
    ("DELETE", "/api/members/{person}"): lambda hh, r, d: logic.remove_member(hh, r.path_params["person"]),
}
NO_BODY = {"/api/bills/{bill_id}/pay", "/api/pantry/{item_id}/use", "/api/members/{person}"}


def register(mcp) -> None:
    """Add the dashboard and API routes to an MCPServer before its app is built."""

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def dashboard(request: Request) -> Response:
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers=SECURITY_HEADERS)

    @mcp.custom_route("/demo", methods=["GET"], include_in_schema=False)
    async def demo(request: Request) -> Response:
        """A real MCP client in the browser: initialize, tools/list and tools/call against /mcp."""
        return HTMLResponse((STATIC / "demo.html").read_text(encoding="utf-8"), headers=SECURITY_HEADERS)

    @mcp.custom_route("/api/demo-token", methods=["GET"])
    async def demo_token(request: Request) -> Response:
        """The public demo household's token, so visitors and judges can try the server without credentials."""
        token = oauth.demo_token()
        if not token:
            return JSONResponse({"error": "No public demo household on this server."}, status_code=404,
                                headers=SECURITY_HEADERS)
        return JSONResponse({"token": token, "household": oauth.DEMO_HOUSEHOLD}, headers=SECURITY_HEADERS)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/api/state", methods=["GET"])
    async def get_state(request: Request) -> Response:
        try:
            hh = _household(request)
        except ApiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status, headers={"WWW-Authenticate": "Bearer"})
        payload = await run_in_threadpool(state, hh)
        tools = await mcp.list_tools()
        payload["connect"] = {
            "mcp_url": mcp_url(request),
            "tools": [{"name": t.name, "read_only": bool(t.annotations and t.annotations.read_only_hint)} for t in tools],
        }
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    for (method, path), action in ACTIONS.items():
        mcp.custom_route(path, methods=[method])(endpoint(action, needs_body=path not in NO_BODY))
