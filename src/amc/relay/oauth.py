"""Authentication for the MCP endpoint.

Two ways in, both ending at the same principal (an AMC agent key):

1. **Agent key as bearer token** — ``Authorization: Bearer amck_...``. Used by
   Claude Code, Codex, scripts, and any client that can send a header.
2. **OAuth 2.1 (authorization code + PKCE, dynamic client registration)** — for
   hosted clients that only do OAuth (Claude.ai / Claude Desktop custom connectors,
   ChatGPT connectors). The consent page asks for an AMC agent key once; the issued
   OAuth tokens act as that key. Removing or rotating the key revokes every token it
   approved (tokens are bound to the approving key's hash, not just its name).

Only hashes of issued tokens are persisted (``relay-oauth.json``).
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from amc.paths import write_private_json

from .store import RelayStore, sha256

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
REQUEST_TTL = 900
MAX_CLIENTS = 2000
MAX_PENDING = 1000


@dataclass
class PendingAuthorization:
    client_id: str
    client_name: str
    params: AuthorizationParams
    expires_at: float


class AmcAuthProvider:
    """``OAuthAuthorizationServerProvider`` + token verifier backed by the relay store."""

    def __init__(self, store: RelayStore, state_path: Path, *, issuer_url: str) -> None:
        self.store = store
        self.state_path = state_path
        self.issuer_url = issuer_url.rstrip("/")
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._code_keys: dict[str, str] = {}
        self._data = self._load()

    # --- persistence --------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        data.setdefault("clients", {})
        data.setdefault("access", {})
        data.setdefault("refresh", {})
        return data

    def _save(self) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            self._data[kind] = {
                digest: item for digest, item in self._data[kind].items() if item.get("expires_at", 0) > now
            }
        write_private_json(self.state_path, json.dumps(self._data, indent=1) + "\n")

    # --- key resolution -----------------------------------------------------------------------

    def key_name_for_secret(self, secret: str) -> str | None:
        client = self.store.state.client_by_key(secret.strip())
        return client.name if client else None

    def _key_exists(self, name: str) -> bool:
        return self.store.state.client_by_name(name) is not None

    def _key_hash(self, name: str) -> str | None:
        client = self.store.state.client_by_name(name)
        return client.key_sha256 if client else None

    def _bound_key_valid(self, item: dict[str, Any]) -> bool:
        """The approving key must still exist *with the same secret* (rotation revokes)."""
        current = self._key_hash(item.get("subject", ""))
        return current is not None and secrets.compare_digest(current, item.get("key_sha256", ""))

    async def load_access_token(self, token: str) -> AccessToken | None:
        name = self.key_name_for_secret(token)
        if name is not None:
            return AccessToken(token=token, client_id=f"key:{name}", scopes=[], subject=name)
        item = self._data["access"].get(sha256(token))
        if item is None or item["expires_at"] <= time.time() or not self._bound_key_valid(item):
            return None
        return AccessToken(
            token=token,
            client_id=item["client_id"],
            scopes=item.get("scopes", []),
            expires_at=int(item["expires_at"]),
            resource=item.get("resource"),
            subject=item["subject"],
        )

    async def verify_token(self, token: str) -> AccessToken | None:  # TokenVerifier protocol
        return await self.load_access_token(token)

    # --- clients (dynamic registration) -------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if len(self._data["clients"]) >= MAX_CLIENTS:
            # Drop the oldest registrations that hold no live token (clients in use are kept).
            in_use = {item["client_id"] for kind in ("access", "refresh") for item in self._data[kind].values()}
            idle = [client_id for client_id in self._data["clients"] if client_id not in in_use]
            if not idle:
                raise RegistrationError(error="invalid_client_metadata", error_description="registration is full")
            for client_id in idle[: MAX_CLIENTS // 10]:
                del self._data["clients"][client_id]
        self._data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self._save()

    # --- authorization ------------------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        while len(self._pending) >= MAX_PENDING:
            # Evict the oldest unfinished login rather than refusing new ones (no lock-out).
            del self._pending[next(iter(self._pending))]
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = PendingAuthorization(
            client_id=client.client_id or "",
            client_name=(client.client_name or client.client_id or "an MCP client")[:80],
            params=params,
            expires_at=now + REQUEST_TTL,
        )
        return f"{self.issuer_url}/oauth/approve?request={request_id}"

    def pending(self, request_id: str) -> PendingAuthorization | None:
        item = self._pending.get(request_id)
        if item is None or item.expires_at <= time.time():
            return None
        return item

    def approve(self, request_id: str, key_secret: str) -> str:
        """Validate the agent key and return the redirect URL carrying the authorization code."""
        item = self.pending(request_id)
        if item is None:
            raise AuthorizeError(error="access_denied", error_description="login request expired; start again")
        name = self.key_name_for_secret(key_secret)
        if name is None:
            raise ValueError("invalid AMC key")
        del self._pending[request_id]
        now = time.time()
        for stale in [c for c, item in self._codes.items() if item.expires_at <= now]:
            self._codes.pop(stale, None)
            self._code_keys.pop(stale, None)
        code = secrets.token_urlsafe(32)
        self._code_keys[code] = self._key_hash(name) or ""
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=item.params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=item.client_id,
            code_challenge=item.params.code_challenge,
            redirect_uri=item.params.redirect_uri,
            redirect_uri_provided_explicitly=item.params.redirect_uri_provided_explicitly,
            resource=item.params.resource,
            subject=name,
        )
        return construct_redirect_uri(str(item.params.redirect_uri), code=code, state=item.params.state)

    def deny(self, request_id: str) -> str | None:
        item = self._pending.pop(request_id, None)
        if item is None:
            return None
        return construct_redirect_uri(
            str(item.params.redirect_uri), error="access_denied", state=item.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at <= time.time():
            return None
        return code

    def _issue(self, *, client_id: str, subject: str, key_sha256: str, scopes: list[str],
               resource: str | None) -> OAuthToken:
        access = "amco_" + secrets.token_urlsafe(32)
        refresh = "amcr_" + secrets.token_urlsafe(32)
        now = time.time()
        base = {"client_id": client_id, "subject": subject, "key_sha256": key_sha256, "scopes": scopes,
                "resource": resource}
        self._data["access"][sha256(access)] = {**base, "expires_at": now + ACCESS_TTL}
        self._data["refresh"][sha256(refresh)] = {**base, "expires_at": now + REFRESH_TTL}
        self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        key_hash = self._code_keys.pop(authorization_code.code, "")
        bound = {"subject": authorization_code.subject or "", "key_sha256": key_hash}
        if not self._bound_key_valid(bound):
            raise TokenError(error="invalid_grant", error_description="approving key no longer exists")
        return self._issue(
            client_id=client.client_id or "",
            subject=authorization_code.subject or "",
            key_sha256=key_hash,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        item = self._data["refresh"].get(sha256(refresh_token))
        if (
            item is None
            or item["client_id"] != client.client_id
            or item["expires_at"] <= time.time()
            or not self._bound_key_valid(item)
        ):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=item["client_id"],
            scopes=item.get("scopes", []),
            expires_at=int(item["expires_at"]),
            resource=item.get("resource"),
            subject=item["subject"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        item = self._data["refresh"].pop(sha256(refresh_token.token), None)  # rotate
        if item is None or not self._bound_key_valid(item):
            raise TokenError(error="invalid_grant", error_description="refresh token revoked")
        return self._issue(
            client_id=client.client_id or "",
            subject=refresh_token.subject or "",
            key_sha256=item["key_sha256"],
            scopes=scopes or refresh_token.scopes,
            resource=refresh_token.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        digest = sha256(token.token)
        self._data["access"].pop(digest, None)
        self._data["refresh"].pop(digest, None)
        self._save()
