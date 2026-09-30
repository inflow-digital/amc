"""Regression tests for the independent security review (each reproduced an exploit before the fix)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.test_e2e import _pair, _payload, mcp_session, running_host

symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")


def _wide_root(env, config):
    """A device whose root also contains the AMC config dir (like `amc up` with root = $HOME)."""
    config.roots = [str(env["tmp"])]
    return config


async def test_c1_device_token_file_is_never_readable(env):
    config = _wide_root(env, _pair(env))
    config.save()  # host.json inside AMC_HOME, which is inside the device root
    reader = env["store"].add_client("reader", access="read")
    async with running_host(config), mcp_session(env["relay"].url, reader) as session:
        for path in (str(config.default_path()), str(env["store"].path)):
            result = _payload(await session.call_tool("amc_read_file", {"device_id": config.device_id, "path": path}))
            assert not result["ok"] and "protected" in json.dumps(result)
            assert config.device_token not in json.dumps(result)
        search = _payload(await session.call_tool("amc_start_search", {
            "device_id": config.device_id, "path": str(env["tmp"]), "pattern": "amcd_", "search_type": "content",
            "include_hidden": True}))
        assert config.device_token not in json.dumps(search)


async def test_c2_write_key_cannot_rewrite_grants(env):
    config = _wide_root(env, _pair(env))
    writer = env["store"].add_client("writer", access="standard")
    async with running_host(config), mcp_session(env["relay"].url, writer) as session:
        result = _payload(await session.call_tool("amc_edit_block", {
            "device_id": config.device_id, "file_path": str(env["store"].path),
            "old_string": '"access": "standard"', "new_string": '"access": "full"'}))
        assert not result["ok"]
    assert env["store"].state.client_by_name("writer").access == "standard"


async def _oauth_token(env, key_secret: str) -> dict:
    base = env["relay"].url
    async with httpx.AsyncClient(base_url=base) as http:
        redirect_uri = "http://127.0.0.1:9/callback"
        reg = (await http.post("/register", json={
            "client_name": "T", "redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})).json()
        verifier = "w" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = await http.get("/authorize", params={
            "response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect_uri,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
        request_id = parse_qs(urlsplit(auth.headers["location"]).query)["request"][0]
        ok = await http.post("/oauth/approve", data={"request": request_id, "key": key_secret, "decision": "allow"})
        code = parse_qs(urlsplit(ok.headers["location"]).query)["code"][0]
        token = (await http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": reg["client_id"], "code_verifier": verifier})).json()
        token["client_id"] = reg["client_id"]
        return token


async def test_c3_rotating_a_key_revokes_its_oauth_tokens(env):
    first = env["store"].add_client("claude-web", access="read")
    token = await _oauth_token(env, first)
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    assert httpx.get(f"{env['relay'].url}/api/v1/devices", headers=headers).status_code == 200
    env["store"].add_client("claude-web", access="full", replace=True)  # rotate: same name, new secret
    assert httpx.get(f"{env['relay'].url}/api/v1/devices", headers=headers).status_code == 401
    refreshed = httpx.post(f"{env['relay'].url}/token", data={
        "grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": token["client_id"]})
    assert refreshed.status_code == 400


@symlinks
async def test_h1_symlink_cannot_escape_key_scope_or_device_root(env):
    config = _pair(env)
    proj = env["work"] / "proj"
    proj.mkdir()
    private = env["work"] / "private"
    private.mkdir()
    (private / "creds.txt").write_text("PRIVATE", encoding="utf-8")
    os.symlink(private, proj / "link")
    os.symlink(env["outside"] / "secret.txt", env["work"] / "outlink.txt")
    scoped = env["store"].add_client("scoped", access="read", paths=[str(proj)])
    wide = env["store"].add_client("wide", access="read")
    async with running_host(config):
        async with mcp_session(env["relay"].url, scoped) as session:
            result = _payload(await session.call_tool("amc_read_file", {
                "device_id": config.device_id, "path": str(proj / "link" / "creds.txt")}))
            assert not result["ok"] and "PRIVATE" not in json.dumps(result)
        async with mcp_session(env["relay"].url, wide) as session:
            found = _payload(await session.call_tool("amc_start_search", {
                "device_id": config.device_id, "path": str(env["work"]), "pattern": "nope",
                "search_type": "content"}))
            assert "outlink" not in json.dumps(found)


async def test_h2_consent_page_names_the_real_destination(env):
    key = env["store"].add_client("k", access="read")
    del key
    async with httpx.AsyncClient(base_url=env["relay"].url) as http:
        reg = (await http.post("/register", json={
            "client_name": "Claude", "redirect_uris": ["https://attacker.example/steal"],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"]})).json()
        auth = await http.get("/authorize", params={
            "response_type": "code", "client_id": reg["client_id"], "redirect_uri": "https://attacker.example/steal",
            "code_challenge": "x" * 43, "code_challenge_method": "S256"})
        request_id = parse_qs(urlsplit(auth.headers["location"]).query)["request"][0]
        page = (await http.get(f"/oauth/approve?request={request_id}")).text
        assert "attacker.example" in page and "Warning" in page


async def test_m1_pending_logins_cannot_lock_out_real_users(env):
    async with httpx.AsyncClient(base_url=env["relay"].url) as http:
        reg = (await http.post("/register", json={
            "client_name": "T", "redirect_uris": ["http://127.0.0.1:9/cb"], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})).json()
        params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "http://127.0.0.1:9/cb",
                  "code_challenge": "x" * 43, "code_challenge_method": "S256"}
        from amc.relay import app as relay_app
        from amc.relay import oauth

        statuses = [(await http.get("/authorize", params=params)).status_code
                    for _ in range(relay_app.OAUTH_REQUESTS_PER_MINUTE + 5)]
        assert statuses[0] == 302 and statuses[-1] == 429  # one address cannot flood pending logins
        assert oauth.MAX_PENDING > relay_app.OAUTH_REQUESTS_PER_MINUTE
