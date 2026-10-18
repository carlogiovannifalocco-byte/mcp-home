"""A small OAuth 2.1 authorization server for account linking (Alexa+ and other MCP clients).

What the Alexa+ MCP Toolkit asks for, and what this module does:

- Protected Resource Metadata (RFC 9728) at /.well-known/oauth-protected-resource (and at the path-inserted
  /.well-known/oauth-protected-resource/mcp), naming this server as the only authorization server.
- Authorization Server Metadata (RFC 8414) at /.well-known/oauth-authorization-server, with S256 in
  code_challenge_methods_supported.
- Authorization code grant with PKCE S256 required. The consent page asks for the household token, so linking an
  account proves the person already belongs to that household.
- Static client registration from the environment (OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_REDIRECT_URIS):
  Alexa does not support Dynamic Client Registration. DCR (RFC 7591) is available for other MCP clients when
  OAUTH_DYNAMIC_REGISTRATION=1.
- A refresh token with every user access token, rotated on use; reuse of an old refresh token or of an
  authorization code revokes the whole grant.
- client_credentials for the static confidential client: a short-lived `mcp:service` token that can initialize and
  list tools but never read or write a household.
- Tokens are opaque random strings; only their SHA-256 is stored (SQLite), with expiry and revocation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import unquote, urlencode, urlsplit

from pydantic import ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientMetadata

from . import db

USER_SCOPE = "household"
SERVICE_SCOPE = "mcp:service"
CODE_TTL = 300
MAX_DCR_CLIENTS = 500
FAILED_LOGINS_ALLOWED = 10
FAILED_LOGIN_WINDOW = 600
PKCE_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
CHALLENGE_RE = re.compile(r"^[A-Za-z0-9\-_]{43}$")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(60, int(os.getenv(name, "")))
    except ValueError:
        return default


def access_ttl() -> int:
    return _env_int("OAUTH_ACCESS_TOKEN_TTL", 3600)


def refresh_ttl() -> int:
    return _env_int("OAUTH_REFRESH_TOKEN_TTL", 90 * 24 * 3600)


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def household_tokens() -> dict[str, str]:
    """HOUSEHOLD_TOKENS="token1:rossi,token2:bianchi" maps bearer tokens to households."""
    pairs = (p.split(":", 1) for p in os.getenv("HOUSEHOLD_TOKENS", "").split(",") if ":" in p)
    return {tok.strip(): hh.strip() for tok, hh in pairs if tok.strip() and hh.strip()}


def household_for_token(token: str) -> str | None:
    """Constant-time lookup of a household token; checks every entry so timing does not reveal which one matched."""
    found = None
    for known, household in household_tokens().items():
        if hmac.compare_digest(known.encode(), token.encode()):
            found = household
    return found


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


def _same_resource(a: str, b: str) -> bool:
    """RFC 8707 resource comparison: scheme and host case-insensitive, a trailing slash aside."""
    pa, pb = urlsplit(a.strip()), urlsplit(b.strip())
    return (
        bool(pa.scheme and pa.netloc) and not pa.fragment
        and (pa.scheme.lower(), pa.netloc.lower(), pa.path.rstrip("/"), pa.query)
        == (pb.scheme.lower(), pb.netloc.lower(), pb.path.rstrip("/"), pb.query)
    )


@dataclass
class Client:
    client_id: str
    redirect_uris: list[str]
    secret_hash: str | None = None
    name: str = ""
    grant_types: set[str] = field(default_factory=lambda: {"authorization_code", "refresh_token"})

    @property
    def confidential(self) -> bool:
        return self.secret_hash is not None


def _static_client() -> Client | None:
    client_id = os.getenv("OAUTH_CLIENT_ID", "").strip()
    if not client_id:
        return None
    secret = os.getenv("OAUTH_CLIENT_SECRET", "")
    uris = [u for u in re.split(r"[,\s]+", os.getenv("OAUTH_REDIRECT_URIS", "")) if u]
    grants = {"authorization_code", "refresh_token"} | ({"client_credentials"} if secret else set())
    return Client(client_id, uris, _hash(secret) if secret else None, os.getenv("OAUTH_CLIENT_NAME", "Alexa"), grants)


def get_client(client_id: str) -> Client | None:
    if not client_id or len(client_id) > 200:
        return None
    static = _static_client()
    if static and hmac.compare_digest(static.client_id.encode(), client_id.encode()):
        return static
    if not _flag("OAUTH_DYNAMIC_REGISTRATION"):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
    if row is None:
        return None
    return Client(row["client_id"], json.loads(row["redirect_uris"]), row["secret_hash"], row["client_name"])


# Token storage --------------------------------------------------------------------------------------------------


def _store_token(conn, kind: str, grant_id: str, client_id: str, household: str | None, scope: str,
                 resource: str, ttl: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO oauth_tokens (token_hash, kind, grant_id, client_id, household, scope, resource, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_hash(token), kind, grant_id, client_id, household, scope, resource, _now() + ttl),
    )
    return token


def _revoke_grant(conn, grant_id: str) -> None:
    conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE grant_id = ?", (grant_id,))
    conn.execute("UPDATE oauth_codes SET used = 1 WHERE grant_id = ?", (grant_id,))


def _issue_user_tokens(conn, grant_id: str, client_id: str, household: str, scope: str, resource: str) -> dict:
    ttl = access_ttl()
    return {
        "access_token": _store_token(conn, "access", grant_id, client_id, household, scope, resource, ttl),
        "token_type": "Bearer",
        "expires_in": ttl,
        "refresh_token": _store_token(conn, "refresh", grant_id, client_id, household, scope, resource, refresh_ttl()),
        "scope": scope,
    }


def load_access_token(token: str, resource: str) -> AccessToken | None:
    """Resolve an OAuth access token issued by this server. Static household tokens are checked by the caller."""
    if not token or len(token) > 512:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'access'", (_hash(token),)
        ).fetchone()
    if row is None or row["revoked"] or row["expires_at"] <= _now() or not _same_resource(row["resource"], resource):
        return None
    scopes = row["scope"].split()
    household = row["household"]
    if USER_SCOPE in scopes:
        # Unlinking a household from HOUSEHOLD_TOKENS also cuts off every app linked to it.
        if not household or household not in household_tokens().values():
            return None
        principal = household
    else:
        principal = f"service:{row['client_id']}"
    return AccessToken(
        token=token, client_id=principal, scopes=scopes, expires_at=row["expires_at"], resource=resource,
        subject=household, claims={"oauth_client_id": row["client_id"]},
    )


# Failed household-token attempts on the consent page -----------------------------------------------------------

_failures: dict[str, deque] = defaultdict(deque)


def _too_many_failures(key: str) -> bool:
    attempts = _failures[key]
    while attempts and attempts[0] < time.monotonic() - FAILED_LOGIN_WINDOW:
        attempts.popleft()
    return len(attempts) >= FAILED_LOGINS_ALLOWED


# HTTP endpoints -------------------------------------------------------------------------------------------------


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


def _json_error(exc: OAuthError, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=exc.status,
                        headers={**NO_STORE, **(headers or {})})


def _page_headers(form_target: str = "") -> dict[str, str]:
    form_action = "'self'" + (f" {form_target}" if form_target else "")
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        **NO_STORE,
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link your household</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--text:#1d1d1b;--muted:#5f5e5a;--line:#dcdad3;--accent:#1f6f5c;--danger:#a33a2b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#171715;--card:#22221f;--text:#ecebe6;--muted:#a9a79f;--line:#3a3935;
--accent:#5fc1a6;--danger:#f08b7a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:440px;margin:8vh auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}}
h1{{font-size:1.35rem;margin:0 0 8px}}p{{margin:0 0 14px;color:var(--muted)}}strong{{color:var(--text)}}
label{{display:block;font-weight:600;margin:18px 0 6px}}
input{{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);
color:var(--text);font:inherit}}
.row{{display:flex;gap:10px;margin-top:18px}}button{{flex:1;padding:11px;border-radius:8px;font:inherit;
font-weight:600;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--text)}}
button.primary{{background:var(--accent);border-color:var(--accent);color:var(--card)}}
.error{{color:var(--danger);font-weight:600}}code{{word-break:break-all}}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""


def _page(body: str, status: int = 200, form_target: str = "") -> HTMLResponse:
    return HTMLResponse(PAGE.format(body=body), status_code=status, headers=_page_headers(form_target))


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    return _page(f"<h1>Cannot link this app</h1><p class='error'>{html.escape(message)}</p>", status)


def cors(handler, methods: list[str]):
    """Browser-based MCP clients (MCP Inspector) call metadata and token endpoints cross-origin. No cookies are
    involved, so a wildcard origin is safe; bodies are capped at 64 KiB."""
    return CORSMiddleware(
        RequestBodyLimitMiddleware(request_response(handler), 64 * 1024),
        allow_origins=["*"], allow_methods=[*methods, "OPTIONS"],
        allow_headers=["authorization", "content-type", "mcp-protocol-version"],
    )


def _origin(uri: str) -> str:
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class AuthServer:
    issuer: str
    resource: str
    resource_name: str = "mcp-home"

    # Metadata ---------------------------------------------------------------------------------------------------

    def protected_resource_metadata(self) -> dict:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": [USER_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": self.resource_name,
        }

    def authorization_server_metadata(self) -> dict:
        meta = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "revocation_endpoint": f"{self.issuer}/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
            "revocation_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
            "scopes_supported": [USER_SCOPE, SERVICE_SCOPE],
            "authorization_response_iss_parameter_supported": True,
        }
        if _flag("OAUTH_DYNAMIC_REGISTRATION"):
            meta["registration_endpoint"] = f"{self.issuer}/register"
        return meta

    async def prm(self, request: Request) -> Response:
        return JSONResponse(self.protected_resource_metadata(), headers={"Cache-Control": "public, max-age=300"})

    async def asm(self, request: Request) -> Response:
        return JSONResponse(self.authorization_server_metadata(), headers={"Cache-Control": "public, max-age=300"})

    # /authorize -------------------------------------------------------------------------------------------------

    def _redirect(self, redirect_uri: str, **params: str | None) -> RedirectResponse:
        query = urlencode({k: v for k, v in {**params, "iss": self.issuer}.items() if v is not None})
        sep = "&" if urlsplit(redirect_uri).query else "?"
        return RedirectResponse(f"{redirect_uri}{sep}{query}", status_code=302, headers=NO_STORE)

    def _consent(self, client: Client, p: dict[str, str], redirect_uri: str, error: str = "",
                 status: int = 200) -> HTMLResponse:
        hidden = "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in p.items() if v is not None
        )
        name = html.escape(client.name or "An app")
        host = html.escape(urlsplit(redirect_uri).netloc)
        body = (
            f"<h1>Link {name} to your household</h1>"
            f"<p><strong>{name}</strong> wants to read and update your household's shopping list, bills, chores "
            f"and pantry.</p><p>After you approve you will be sent back to <strong>{host}</strong>.</p>"
            + (f"<p class='error' role='alert'>{html.escape(error)}</p>" if error else "")
            + f'<form method="post" action="/authorize">{hidden}'
            '<label for="t">Household token</label>'
            '<input id="t" name="household_token" type="password" autocomplete="off" required '
            'spellcheck="false" autofocus>'
            '<div class="row"><button type="submit" name="action" value="deny" formnovalidate>Deny</button>'
            '<button class="primary" type="submit" name="action" value="approve">Allow</button></div></form>'
        )
        return _page(body, status, form_target=_origin(redirect_uri))

    async def authorize(self, request: Request) -> Response:
        raw = request.query_params if request.method == "GET" else await request.form()
        keys = ("response_type", "client_id", "redirect_uri", "scope", "state", "code_challenge",
                "code_challenge_method", "resource")
        p = {k: v for k in keys if isinstance(v := raw.get(k), str)}

        client = get_client(p.get("client_id", ""))
        if client is None:
            return _error_page("Unknown client_id.")
        redirect_uri = p.get("redirect_uri")
        if redirect_uri is None and len(client.redirect_uris) == 1:
            redirect_uri = client.redirect_uris[0]
        if not redirect_uri or not any(hmac.compare_digest(redirect_uri.encode(), u.encode())
                                       for u in client.redirect_uris):
            # Never redirect to an address the client did not register (RFC 6749 4.1.2.1).
            return _error_page("redirect_uri is not registered for this client.")

        state = p.get("state")

        def fail(error: str, description: str) -> RedirectResponse:
            return self._redirect(redirect_uri, error=error, error_description=description, state=state)

        if p.get("response_type") != "code":
            return fail("unsupported_response_type", "Only response_type=code is supported.")
        if "authorization_code" not in client.grant_types:
            return fail("unauthorized_client", "This client may not use the authorization code grant.")
        if p.get("code_challenge_method") != "S256" or not CHALLENGE_RE.match(p.get("code_challenge", "")):
            return fail("invalid_request", "PKCE is required: send code_challenge with code_challenge_method=S256.")
        if p.get("resource") and not _same_resource(p["resource"], self.resource):
            return fail("invalid_target", f"Unknown resource; this server is {self.resource}.")

        if request.method == "GET":
            return self._consent(client, p, redirect_uri)
        if raw.get("action") != "approve":
            return fail("access_denied", "The household did not allow access.")

        who = request.client.host if request.client else "unknown"
        if _too_many_failures(who):
            return self._consent(client, p, redirect_uri, "Too many wrong tokens. Try again in a few minutes.", 429)
        entered = raw.get("household_token")
        household = household_for_token(entered.strip()) if isinstance(entered, str) and entered.strip() else None
        if household is None:
            _failures[who].append(time.monotonic())
            return self._consent(client, p, redirect_uri, "That household token is not valid.", 401)

        code = secrets.token_urlsafe(32)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, household, redirect_uri, code_challenge, scope, "
                "resource, grant_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_hash(code), client.client_id, household, redirect_uri, p["code_challenge"], USER_SCOPE,
                 self.resource, secrets.token_hex(16), _now() + CODE_TTL),
            )
        return self._redirect(redirect_uri, code=code, state=state)

    # /token -----------------------------------------------------------------------------------------------------

    @staticmethod
    def _authenticate_client(request: Request, form) -> Client:
        """client_secret_basic, client_secret_post, or none for public clients. Never from the query string."""
        if "client_secret" in request.query_params or "client_id" in request.query_params:
            raise OAuthError("invalid_request", "Client credentials must not be sent in the query string.")
        form_id = form.get("client_id") if isinstance(form.get("client_id"), str) else None
        secret = form.get("client_secret") if isinstance(form.get("client_secret"), str) else None
        client_id = form_id
        header = request.headers.get("authorization", "")
        if header[:6].lower() == "basic ":
            try:
                basic_id, basic_secret = base64.b64decode(header[6:], validate=True).decode().split(":", 1)
            except (ValueError, UnicodeDecodeError):
                raise OAuthError("invalid_client", "Malformed Basic authorization header.", 401) from None
            client_id, secret = unquote(basic_id), unquote(basic_secret)
            if form_id is not None and form_id != client_id:
                raise OAuthError("invalid_client", "client_id does not match the Basic credentials.", 401)
        client = get_client(client_id or "")
        if client is None:
            raise OAuthError("invalid_client", "Unknown client.", 401)
        if client.confidential and not (secret and hmac.compare_digest(_hash(secret), client.secret_hash or "")):
            raise OAuthError("invalid_client", "Client authentication failed.", 401)
        return client

    def _check_resource(self, form) -> None:
        resource = form.get("resource")
        if isinstance(resource, str) and resource and not _same_resource(resource, self.resource):
            raise OAuthError("invalid_target", f"Unknown resource; this server is {self.resource}.")

    def _authorization_code(self, client: Client, form) -> dict:
        code, verifier = form.get("code"), form.get("code_verifier")
        if not isinstance(code, str) or not code:
            raise OAuthError("invalid_request", "Missing code.")
        if not isinstance(verifier, str) or not PKCE_RE.match(verifier):
            raise OAuthError("invalid_request", "Missing or malformed code_verifier (PKCE is required).")
        self._check_resource(form)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (_hash(code),)).fetchone()
            if row is None or not hmac.compare_digest(row["client_id"].encode(), client.client_id.encode()):
                raise OAuthError("invalid_grant", "Unknown authorization code.")
            # Single use: claim the code atomically. A second redemption means it leaked: revoke what it issued.
            claimed = conn.execute("UPDATE oauth_codes SET used = 1 WHERE code_hash = ? AND used = 0",
                                   (row["code_hash"],)).rowcount == 1
            if not claimed:
                _revoke_grant(conn, row["grant_id"])
                conn.commit()
                raise OAuthError("invalid_grant", "Authorization code was already used.")
            conn.commit()  # the code is spent even if a check below fails (a wrong verifier burns it)
            if row["expires_at"] <= _now():
                raise OAuthError("invalid_grant", "Authorization code has expired.")
            redirect_uri = form.get("redirect_uri")
            if isinstance(redirect_uri, str) and redirect_uri != row["redirect_uri"]:
                raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request.")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if not hmac.compare_digest(challenge, row["code_challenge"]):
                raise OAuthError("invalid_grant", "code_verifier does not match the code_challenge.")
            if row["household"] not in household_tokens().values():
                raise OAuthError("invalid_grant", "This household is no longer configured.")
            return _issue_user_tokens(conn, row["grant_id"], client.client_id, row["household"], row["scope"],
                                      row["resource"])

    def _refresh_token(self, client: Client, form) -> dict:
        token = form.get("refresh_token")
        if not isinstance(token, str) or not token:
            raise OAuthError("invalid_request", "Missing refresh_token.")
        self._check_resource(form)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                               (_hash(token),)).fetchone()
            if row is None or not hmac.compare_digest(row["client_id"].encode(), client.client_id.encode()):
                raise OAuthError("invalid_grant", "Unknown refresh token.")
            if row["revoked"]:
                # Rotation: each refresh token works once. Seeing a used one again means it was stolen.
                _revoke_grant(conn, row["grant_id"])
                conn.commit()
                raise OAuthError("invalid_grant", "Refresh token was already used or revoked.")
            if row["expires_at"] <= _now():
                raise OAuthError("invalid_grant", "Refresh token has expired; link the account again.")
            if row["household"] not in household_tokens().values():
                raise OAuthError("invalid_grant", "This household is no longer configured.")
            requested = form.get("scope")
            if isinstance(requested, str) and requested.strip() and not set(requested.split()) <= set(row["scope"].split()):
                raise OAuthError("invalid_scope", "A refresh cannot add scopes.")
            scope = " ".join(requested.split()) if isinstance(requested, str) and requested.strip() else row["scope"]
            claimed = conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ? AND revoked = 0",
                                   (row["token_hash"],)).rowcount == 1
            if not claimed:  # a concurrent refresh with the same token won the race
                raise OAuthError("invalid_grant", "Refresh token was already used.")
            return _issue_user_tokens(conn, row["grant_id"], client.client_id, row["household"], scope,
                                      row["resource"])

    def _client_credentials(self, client: Client, form) -> dict:
        if not client.confidential or "client_credentials" not in client.grant_types:
            raise OAuthError("unauthorized_client", "client_credentials needs the pre-registered confidential client.")
        requested = form.get("scope")
        if isinstance(requested, str) and requested.strip() and set(requested.split()) - {SERVICE_SCOPE}:
            raise OAuthError("invalid_scope", f"client_credentials only grants {SERVICE_SCOPE}.")
        self._check_resource(form)
        ttl = min(access_ttl(), 3600)
        with db.connect() as conn:
            token = _store_token(conn, "access", secrets.token_hex(16), client.client_id, None, SERVICE_SCOPE,
                                 self.resource, ttl)
        return {"access_token": token, "token_type": "Bearer", "expires_in": ttl, "scope": SERVICE_SCOPE}

    async def token(self, request: Request) -> Response:
        form = await request.form()
        try:
            client = self._authenticate_client(request, form)
            grant = form.get("grant_type")
            if grant not in client.grant_types or grant not in (
                "authorization_code", "refresh_token", "client_credentials"
            ):
                raise OAuthError("unsupported_grant_type", f"grant_type {grant!r} is not available to this client.")
            handler = {"authorization_code": self._authorization_code, "refresh_token": self._refresh_token,
                       "client_credentials": self._client_credentials}[grant]
            return JSONResponse(handler(client, form), headers=NO_STORE)
        except OAuthError as exc:
            basic = request.headers.get("authorization", "")[:6].lower() == "basic "
            return _json_error(exc, {"WWW-Authenticate": "Basic"} if exc.status == 401 and basic else None)

    # /revoke (RFC 7009) -----------------------------------------------------------------------------------------

    async def revoke(self, request: Request) -> Response:
        form = await request.form()
        try:
            client = self._authenticate_client(request, form)
        except OAuthError as exc:
            return _json_error(exc)
        token = form.get("token")
        if isinstance(token, str) and token:
            with db.connect() as conn:
                row = conn.execute("SELECT grant_id, client_id FROM oauth_tokens WHERE token_hash = ?",
                                   (_hash(token),)).fetchone()
                if row and hmac.compare_digest(row["client_id"].encode(), client.client_id.encode()):
                    _revoke_grant(conn, row["grant_id"])
        return Response(status_code=200, headers=NO_STORE)

    # /register (RFC 7591), opt-in -------------------------------------------------------------------------------

    async def register(self, request: Request) -> Response:
        if not _flag("OAUTH_DYNAMIC_REGISTRATION"):
            return _json_error(OAuthError("access_denied", "Dynamic client registration is disabled.", 404))
        try:
            meta = OAuthClientMetadata.model_validate_json(await request.body())
        except ValidationError as exc:
            return _json_error(OAuthError("invalid_client_metadata", str(exc.errors()[0].get("msg", "invalid"))))
        uris = [str(u) for u in meta.redirect_uris or []]
        for uri in uris:
            parts = urlsplit(uri)
            loopback = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
            if parts.fragment or not (parts.scheme == "https" or loopback):
                return _json_error(OAuthError("invalid_redirect_uri", "Redirect URIs must be HTTPS or loopback."))
        method = meta.token_endpoint_auth_method or "client_secret_basic"
        if method not in ("none", "client_secret_basic", "client_secret_post"):
            return _json_error(OAuthError("invalid_client_metadata", f"Unsupported auth method {method}."))
        if "authorization_code" not in meta.grant_types or set(meta.grant_types) - {"authorization_code",
                                                                                     "refresh_token"}:
            return _json_error(OAuthError("invalid_client_metadata",
                                          "grant_types must be authorization_code (and refresh_token)."))
        client_id, secret = secrets.token_urlsafe(18), None if method == "none" else secrets.token_urlsafe(32)
        name = (meta.client_name or "")[:80]
        with db.connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] >= MAX_DCR_CLIENTS:
                return _json_error(OAuthError("temporarily_unavailable", "Too many registered clients.", 503))
            conn.execute(
                "INSERT INTO oauth_clients (client_id, secret_hash, redirect_uris, client_name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (client_id, _hash(secret) if secret else None, json.dumps(uris), name, _now()),
            )
        body = {
            "client_id": client_id, "client_id_issued_at": _now(), "client_name": name, "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": method, "scope": USER_SCOPE,
        }
        if secret:
            body |= {"client_secret": secret, "client_secret_expires_at": 0}
        return JSONResponse(body, status_code=201, headers=NO_STORE)

    def routes(self) -> list[Route]:
        """Starlette routes for the metadata documents and the authorization server endpoints.

        The two protected-resource paths go in front of the SDK's own route so they also carry scopes_supported.
        """
        resource_path = urlsplit(self.resource).path.rstrip("/")
        return [
            Route("/.well-known/oauth-protected-resource", cors(self.prm, ["GET"]), methods=["GET", "OPTIONS"]),
            Route(f"/.well-known/oauth-protected-resource{resource_path}", cors(self.prm, ["GET"]),
                  methods=["GET", "OPTIONS"]),
            Route("/.well-known/oauth-authorization-server", cors(self.asm, ["GET"]), methods=["GET", "OPTIONS"]),
            Route("/authorize", RequestBodyLimitMiddleware(request_response(self.authorize), 64 * 1024),
                  methods=["GET", "POST"]),
            Route("/token", cors(self.token, ["POST"]), methods=["POST", "OPTIONS"]),
            Route("/revoke", cors(self.revoke, ["POST"]), methods=["POST", "OPTIONS"]),
            Route("/register", cors(self.register, ["POST"]), methods=["POST", "OPTIONS"]),
        ]
