"""OAuth 2.1 account linking, end to end over HTTP: metadata, consent, code + PKCE, refresh, revocation, isolation."""

import asyncio
import base64
import hashlib
import json
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mcp_home import oauth

ALEXA_REDIRECT = "https://alexa.amazon.com/api/skill/link/M3IHEJ1OZMSGZ3"
CLIENT_ID, CLIENT_SECRET = "alexa-skill", "s3cret-for-tests"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def alexa_client(monkeypatch):
    monkeypatch.setenv("OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("OAUTH_REDIRECT_URIS", f"{ALEXA_REDIRECT}, https://layla.amazon.com/api/skill/link/M3IHEJ1OZMSGZ3")
    monkeypatch.delenv("OAUTH_DYNAMIC_REGISTRATION", raising=False)
    oauth._failures.clear()


@pytest.fixture
def resource(base_url):
    return httpx.get(f"{base_url}/.well-known/oauth-protected-resource").json()["resource"]


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_params(resource, challenge, client_id=CLIENT_ID, redirect_uri=ALEXA_REDIRECT, **extra):
    return {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": "household",
            "state": "abc123", "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource,
            **extra}


def get_code(base_url, resource, challenge, household_token="tok-rossi", **kw):
    params = authorize_params(resource, challenge, **kw)
    page = httpx.get(f"{base_url}/authorize", params=params)
    assert page.status_code == 200, page.text
    r = httpx.post(f"{base_url}/authorize", data={**params, "household_token": household_token, "action": "approve"})
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(kw.get("redirect_uri", ALEXA_REDIRECT) + "?")
    assert q["state"] == ["abc123"]
    return q["code"][0]


def token_request(base_url, data, basic=True):
    auth = (CLIENT_ID, CLIENT_SECRET) if basic else None
    return httpx.post(f"{base_url}/token", data=data, auth=auth)


def link(base_url, resource, household_token="tok-rossi"):
    verifier, challenge = pkce()
    code = get_code(base_url, resource, challenge, household_token)
    r = token_request(base_url, {"grant_type": "authorization_code", "code": code, "redirect_uri": ALEXA_REDIRECT,
                                 "client_id": CLIENT_ID, "code_verifier": verifier, "resource": resource})
    assert r.status_code == 200, r.text
    return r.json()


def rpc(server, token, method, params=None):
    return httpx.post(server, headers={**HEADERS, "Authorization": f"Bearer {token}"},
                      json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call(server, token, name, args=None):
    r = rpc(server, token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def data(server, token, name, args=None):
    result = call(server, token, name, args)
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


# Metadata ---------------------------------------------------------------------------------------------------------


def test_protected_resource_and_authorization_server_metadata(base_url):
    root = httpx.get(f"{base_url}/.well-known/oauth-protected-resource", headers={"Origin": "http://localhost:6274"})
    path = httpx.get(f"{base_url}/.well-known/oauth-protected-resource/mcp")
    assert root.status_code == path.status_code == 200 and root.json() == path.json()
    prm = root.json()
    assert prm["resource"].endswith("/mcp") and prm["scopes_supported"] == ["household"]
    issuer = prm["authorization_servers"][0]
    asm = httpx.get(f"{base_url}/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == issuer
    assert asm["authorization_endpoint"] == f"{issuer}/authorize" and asm["token_endpoint"] == f"{issuer}/token"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["response_types_supported"] == ["code"]
    assert {"authorization_code", "refresh_token", "client_credentials"} <= set(asm["grant_types_supported"])
    assert "registration_endpoint" not in asm  # Alexa uses static registration; DCR is opt-in
    assert root.headers["access-control-allow-origin"] == "*"


def test_unauthenticated_mcp_points_at_resource_metadata(server):
    r = httpx.post(server, headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]


# Authorization code + PKCE ----------------------------------------------------------------------------------------


def test_consent_page_shows_where_the_code_goes_and_is_locked_down(base_url, resource):
    page = httpx.get(f"{base_url}/authorize", params=authorize_params(resource, pkce()[1]))
    assert page.status_code == 200
    assert "alexa.amazon.com" in page.text and 'type="password"' in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self' https://alexa.amazon.com" in csp
    assert page.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("basic", [True, False], ids=["client_secret_basic", "client_secret_post"])
def test_full_code_flow_then_mcp_calls(base_url, server, resource, basic):
    verifier, challenge = pkce()
    code = get_code(base_url, resource, challenge)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": ALEXA_REDIRECT, "client_id": CLIENT_ID,
            "code_verifier": verifier, "resource": resource}
    if not basic:
        form["client_secret"] = CLIENT_SECRET
    r = token_request(base_url, form, basic=basic)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    tokens = r.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["refresh_token"]
    assert tokens["scope"] == "household"

    access = tokens["access_token"]
    init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "alexa", "version": "1"}}
    assert rpc(server, access, "initialize", init).json()["result"]["protocolVersion"] == "2025-11-25"
    assert not call(server, access, "add_grocery_item", {"item": "oat milk"}).get("isError")
    assert data(server, "tok-rossi", "get_grocery_list")["count"] == 1
    assert data(server, "tok-bianchi", "get_grocery_list")["count"] == 0
    state = httpx.get(f"{base_url}/api/state", headers={"Authorization": f"Bearer {access}"}).json()
    assert state["household"] == "rossi"


def test_wrong_verifier_is_rejected_and_burns_the_code(base_url, resource):
    verifier, challenge = pkce()
    code = get_code(base_url, resource, challenge)
    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": ALEXA_REDIRECT, "client_id": CLIENT_ID}
    r = token_request(base_url, {**base, "code_verifier": pkce()[0]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert token_request(base_url, {**base, "code_verifier": verifier}).json()["error"] == "invalid_grant"


def test_missing_verifier_and_mismatched_redirect_are_rejected(base_url, resource):
    verifier, challenge = pkce()
    code = get_code(base_url, resource, challenge)
    r = token_request(base_url, {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID})
    assert r.json()["error"] == "invalid_request"
    code = get_code(base_url, resource, challenge)
    r = token_request(base_url, {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                 "redirect_uri": "https://layla.amazon.com/api/skill/link/M3IHEJ1OZMSGZ3"})
    assert r.json()["error"] == "invalid_grant"


def test_code_reuse_revokes_the_tokens_it_issued(base_url, server, resource):
    verifier, challenge = pkce()
    code = get_code(base_url, resource, challenge)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": ALEXA_REDIRECT,
            "client_id": CLIENT_ID, "code_verifier": verifier}
    first = token_request(base_url, data).json()
    assert rpc(server, first["access_token"], "tools/list").status_code == 200
    assert token_request(base_url, data).json()["error"] == "invalid_grant"
    assert rpc(server, first["access_token"], "tools/list").status_code == 401


def test_expired_code_is_rejected(base_url, resource, tmp_path):
    verifier, challenge = pkce()
    code = get_code(base_url, resource, challenge)
    with sqlite3.connect(tmp_path / "home.db") as conn:
        conn.execute("UPDATE oauth_codes SET expires_at = 0")
    r = token_request(base_url, {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
                                 "code_verifier": verifier})
    assert r.json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "change,error",
    [({"code_challenge_method": "plain"}, "invalid_request"), ({"code_challenge": ""}, "invalid_request"),
     ({"response_type": "token"}, "unsupported_response_type"),
     ({"resource": "https://evil.example.com/mcp"}, "invalid_target")],
)
def test_bad_authorization_requests_redirect_with_error_and_state(base_url, resource, change, error):
    params = {**authorize_params(resource, pkce()[1]), **change}
    r = httpx.get(f"{base_url}/authorize", params=params)
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(ALEXA_REDIRECT) and q["error"] == [error] and q["state"] == ["abc123"]


@pytest.mark.parametrize("change", [{"client_id": "someone-else"}, {"redirect_uri": "https://evil.example.com/cb"},
                                    {"redirect_uri": ALEXA_REDIRECT + "/x"}])
def test_unknown_client_or_redirect_is_never_redirected_to(base_url, resource, change):
    params = {**authorize_params(resource, pkce()[1]), **change}
    for r in (httpx.get(f"{base_url}/authorize", params=params),
              httpx.post(f"{base_url}/authorize", data={**params, "household_token": "tok-rossi", "action": "approve"})):
        assert r.status_code == 400 and "location" not in r.headers


def test_wrong_household_token_and_deny(base_url, resource):
    params = authorize_params(resource, pkce()[1])
    r = httpx.post(f"{base_url}/authorize", data={**params, "household_token": "tok-rossiX", "action": "approve"})
    assert r.status_code == 401 and "location" not in r.headers and "not valid" in r.text
    r = httpx.post(f"{base_url}/authorize", data={**params, "action": "deny"})
    assert r.status_code == 302 and parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["access_denied"]


def test_repeated_wrong_household_tokens_are_throttled(base_url, resource):
    params = {**authorize_params(resource, pkce()[1]), "household_token": "guess", "action": "approve"}
    codes = [httpx.post(f"{base_url}/authorize", data=params).status_code for _ in range(oauth.FAILED_LOGINS_ALLOWED + 1)]
    assert codes[-1] == 429
    r = httpx.post(f"{base_url}/authorize", data={**params, "household_token": "tok-rossi"})
    assert r.status_code == 429  # even the right token waits until the window passes


# Client authentication --------------------------------------------------------------------------------------------


def test_client_authentication_is_enforced(base_url):
    r = httpx.post(f"{base_url}/token", data={"grant_type": "client_credentials"}, auth=(CLIENT_ID, "wrong"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_client" and r.headers["www-authenticate"] == "Basic"
    r = httpx.post(f"{base_url}/token", data={"grant_type": "client_credentials", "client_id": CLIENT_ID})
    assert r.status_code == 401
    r = httpx.post(f"{base_url}/token", params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
                   data={"grant_type": "client_credentials"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


# Refresh, expiry, revocation --------------------------------------------------------------------------------------


def test_refresh_rotates_and_reuse_revokes_the_grant(base_url, server, resource):
    first = link(base_url, resource)
    r = token_request(base_url, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"],
                                 "client_id": CLIENT_ID})  # Alexa sends no `resource` on refresh
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"] and second["access_token"] != first["access_token"]
    assert rpc(server, second["access_token"], "tools/list").status_code == 200
    replay = token_request(base_url, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    assert replay.json()["error"] == "invalid_grant"
    assert rpc(server, second["access_token"], "tools/list").status_code == 401
    again = token_request(base_url, {"grant_type": "refresh_token", "refresh_token": second["refresh_token"]})
    assert again.json()["error"] == "invalid_grant"


def test_expired_access_token_is_rejected(base_url, server, resource, tmp_path):
    tokens = link(base_url, resource)
    assert rpc(server, tokens["access_token"], "tools/list").status_code == 200
    with sqlite3.connect(tmp_path / "home.db") as conn:
        conn.execute("UPDATE oauth_tokens SET expires_at = 1 WHERE kind = 'access'")
    assert rpc(server, tokens["access_token"], "tools/list").status_code == 401
    # the refresh token still works and yields a fresh access token
    r = token_request(base_url, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert rpc(server, r.json()["access_token"], "tools/list").status_code == 200


def test_revoked_token_is_rejected(base_url, server, resource):
    tokens = link(base_url, resource)
    r = httpx.post(f"{base_url}/revoke", data={"token": tokens["refresh_token"]}, auth=(CLIENT_ID, CLIENT_SECRET))
    assert r.status_code == 200
    assert rpc(server, tokens["access_token"], "tools/list").status_code == 401
    r = token_request(base_url, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert r.json()["error"] == "invalid_grant"


def test_unconfiguring_a_household_cuts_off_its_linked_apps(base_url, server, resource, monkeypatch):
    tokens = link(base_url, resource)
    monkeypatch.setenv("HOUSEHOLD_TOKENS", "tok-bianchi:bianchi")
    assert rpc(server, tokens["access_token"], "tools/list").status_code == 401


def test_tokens_are_stored_hashed(base_url, resource, tmp_path):
    tokens = link(base_url, resource)
    dump = "\n".join(sqlite3.connect(tmp_path / "home.db").iterdump())
    assert tokens["access_token"] not in dump and tokens["refresh_token"] not in dump
    assert hashlib.sha256(tokens["access_token"].encode()).hexdigest() in dump


# Household isolation ----------------------------------------------------------------------------------------------


def test_oauth_tokens_are_scoped_per_household(base_url, server, resource):
    rossi = link(base_url, resource, "tok-rossi")["access_token"]
    bianchi = link(base_url, resource, "tok-bianchi")["access_token"]
    call(server, rossi, "add_bill", {"provider": "Enel", "amount": 80, "due": "tomorrow"})
    assert data(server, rossi, "upcoming_deadlines")["bills"][0]["provider"] == "Enel"
    assert data(server, bianchi, "upcoming_deadlines")["bills"] == []
    bill_id = data(server, rossi, "upcoming_deadlines")["bills"][0]["id"]
    r = httpx.post(f"{base_url}/api/bills/{bill_id}/pay", headers={"Authorization": f"Bearer {bianchi}"})
    assert r.json()["result"]["status"] == "not_found"


# Service-level token (Alexa+ Tier 1) ------------------------------------------------------------------------------


def test_client_credentials_token_lists_tools_but_reads_no_household(base_url, server, resource):
    r = httpx.post(f"{base_url}/token", data={"grant_type": "client_credentials", "resource": resource},
                   auth=(CLIENT_ID, CLIENT_SECRET))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "mcp:service" and body["expires_in"] <= 3600 and "refresh_token" not in body
    service = body["access_token"]
    assert len(rpc(server, service, "tools/list").json()["result"]["tools"]) == 16
    result = call(server, service, "get_grocery_list")
    assert result["isError"] and "link" in result["content"][0]["text"].lower()
    assert httpx.get(f"{base_url}/api/state", headers={"Authorization": f"Bearer {service}"}).status_code == 401


# Dynamic client registration (opt-in, for MCP clients other than Alexa) -------------------------------------------


def test_dynamic_registration_is_off_by_default(base_url):
    r = httpx.post(f"{base_url}/register", json={"redirect_uris": ["http://localhost:6274/oauth/callback"]})
    assert r.status_code == 404


def test_dynamic_registration_public_client_flow(base_url, server, resource, monkeypatch):
    monkeypatch.setenv("OAUTH_DYNAMIC_REGISTRATION", "1")
    asm = httpx.get(f"{base_url}/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"].endswith("/register")
    bad = httpx.post(f"{base_url}/register", json={"redirect_uris": ["http://evil.example.com/cb"]})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"
    callback = "http://localhost:6274/oauth/callback"
    reg = httpx.post(f"{base_url}/register", json={"redirect_uris": [callback], "client_name": "MCP Inspector",
                                                   "token_endpoint_auth_method": "none"})
    assert reg.status_code == 201 and "client_secret" not in reg.json()
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()
    code = get_code(base_url, resource, challenge, client_id=client_id, redirect_uri=callback)
    r = httpx.post(f"{base_url}/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                              "redirect_uri": callback, "code_verifier": verifier, "resource": resource})
    assert r.status_code == 200, r.text
    assert data(server, r.json()["access_token"], "weekly_briefing")["speech"]
    # a public client cannot use client_credentials
    r = httpx.post(f"{base_url}/token", data={"grant_type": "client_credentials", "client_id": client_id})
    assert r.json()["error"] == "unsupported_grant_type"


# WWW_AUTHENTICATE=off -----------------------------------------------------------------------------------------------


def test_without_401_challenge_strips_header_only_on_mcp():
    from mcp_home.server import Without401Challenge

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"www-authenticate", b"Bearer"), (b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})

    async def run(path):
        sent = []

        async def send(message):
            sent.append(message)

        await Without401Challenge(inner)({"type": "http", "path": path}, None, send)
        return dict(sent[0]["headers"])

    assert b"www-authenticate" not in asyncio.run(run("/mcp"))
    assert b"www-authenticate" in asyncio.run(run("/api/state"))
