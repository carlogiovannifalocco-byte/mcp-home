"""The /demo page: a real MCP client in the browser (initialize, tools/list, tools/call over Streamable HTTP)."""

import os
import shutil

import httpx
import pytest


def test_demo_page_is_served_locked_down_and_linked(base_url):
    r = httpx.get(f"{base_url}/demo")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["cache-control"] == "no-store"
    assert "tok-rossi" not in r.text
    for method in ("initialize", "notifications/initialized", "tools/list", "tools/call"):
        assert f'"{method}"' in r.text
    assert 'protocolVersion: PROTOCOL' in r.text and '"2025-11-25"' in r.text
    assert "onclick=" not in r.text.lower()  # listeners only, no inline handlers
    dashboard = httpx.get(f"{base_url}/").text
    assert dashboard.count('href="/demo"') >= 2  # header and the Connect panel


def _chromium():
    pytest.importorskip("playwright.sync_api")
    for path in (os.getenv("PLAYWRIGHT_CHROMIUM"), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                 shutil.which("chromium"), shutil.which("google-chrome")):
        if path and os.path.exists(path):
            return path
    return None  # let Playwright use its own download, if any


def test_demo_page_drives_the_real_mcp_endpoint(base_url):
    from playwright.sync_api import Error, sync_playwright

    executable = _chromium()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=executable) if executable else p.chromium.launch()
        except Error as exc:
            pytest.skip(f"no Chromium for Playwright: {exc}")
        page = browser.new_context().new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base_url}/demo")
        page.fill("#token-input", "tok-rossi")
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#conn-pill.on", timeout=10000)
        assert "2025-11-25" in page.inner_text("#s-proto")
        assert page.locator("#tools li").count() == 18
        assert page.locator("#tools .tag.ro").count() == 7

        page.fill("#say", "Add oat milk to the shopping list")
        page.press("#say", "Enter")
        page.wait_for_selector(".turn .spoken:not(.pending)", timeout=10000)
        assert "Added oat milk to the shopping list" in page.inner_text(".turn .spoken")
        assert page.inner_text(".turn .tag.tool") == "add_grocery_item"
        assert page.inner_text(".turn .tag.fast").endswith(" ms")

        page.fill("#say", "Whose turn is it to take out the trash?")
        page.press("#say", "Enter")
        page.wait_for_selector(".turn:nth-child(1) .tag.tool:has-text('whose_turn')", timeout=10000)

        # Paying a bill: the server reads it back, and only the following "yes" pays it.
        httpx.post(f"{base_url}/api/bills", headers={"Authorization": "Bearer tok-rossi"},
                   json={"provider": "Gas", "amount": 80, "due": "tomorrow"})
        page.fill("#say", "We paid the gas bill")
        page.press("#say", "Enter")
        page.wait_for_selector(".turn:nth-child(1) .tag:has-text('waiting for yes')", timeout=10000)
        assert page.inner_text(".turn:nth-child(1) .spoken").startswith("Mark the Gas bill of 80 euro, due tomorrow, as paid?")
        page.fill("#say", "Yes")
        page.press("#say", "Enter")
        page.wait_for_selector(".turn:nth-child(1) .spoken:has-text('Marked the Gas bill')", timeout=10000)
        browser.close()
    assert errors == []
    state = httpx.get(f"{base_url}/api/state", headers={"Authorization": "Bearer tok-rossi"}).json()
    assert [g["item"] for g in state["groceries"]] == ["oat milk"]
    assert state["bills"]["bills"] == []


def test_free_sentence_without_bedrock_explains_itself(server, monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    r = httpx.post(server, headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                                    "Authorization": "Bearer tok-rossi"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "do_from_sentence", "arguments": {"sentence": "we are out of milk"}}})
    result = r.json()["result"]
    assert result["isError"] and "BEDROCK_MODEL_ID" in result["content"][0]["text"]
