"""Verify short-lived CRM-issued grants; no caller-controlled identity is trusted.

This primitive deliberately does not enable retrieval or delegate to subagents.
A verified grant still requires authoritative session, agent existence, delegation,
event freshness, replay protection, tenant and current binding checks.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

import jwt
import hashlib
import hmac
import json
import os


class InvalidGrant(ValueError):
    """Grant verification failed; callers must deny access without fallback."""


def configured_public_keys() -> dict[str, str]:
    """Load the active key and explicitly trusted rotation keys from config."""
    try:
        kid = os.environ["KNOWLEDGE_RUNTIME_GRANT_KID"]
        current = os.environ["KNOWLEDGE_RUNTIME_GRANT_PUBLIC_KEY"]
        old = json.loads(os.getenv("KNOWLEDGE_RUNTIME_HISTORY_PUBLIC_KEYS", "{}"))
        if (not kid or not current or not isinstance(old, dict) or len(old) > 10
                or kid in old or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                                     for k, v in old.items())):
            raise InvalidGrant("unconfigured_verifier")
        return {**old, kid: current}
    except (KeyError, ValueError, TypeError):
        raise InvalidGrant("unconfigured_verifier") from None


@dataclass(frozen=True, slots=True)
class VerifiedGrant:
    account_scope: str
    conversation_id: str
    inbox_id: str
    contact_id: str
    bot_id: str
    root_agent_id: str
    event_id: str
    issued_at: int
    expires_at: int
    grant_id: str


class GrantVerifier:
    """Verify only RS256 grants using an explicitly configured key ring."""

    def __init__(self, *, public_keys: Mapping[str, str], issuer: str, audience: str, expected_account_scope: str):
        if not public_keys or not issuer or not audience or not isinstance(expected_account_scope, str) or not expected_account_scope:
            raise InvalidGrant("unconfigured_verifier")
        self._keys = MappingProxyType(dict(public_keys))
        self._issuer = issuer
        self._audience = audience
        self._account_scope = expected_account_scope

    def verify(self, token: str, *, expected_root_agent_id: str, expected_conversation_id: str, expected_contact_id: str, expected_request_sha256: str | None = None) -> VerifiedGrant:
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 8192:
                raise InvalidGrant("invalid_grant")
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise InvalidGrant("invalid_grant")
            key = self._keys.get(header["kid"])
            if not key:
                raise InvalidGrant("invalid_grant")
            required = ["iss", "aud", "iat", "exp", "jti", "account_scope", "conversation_id", "inbox_id", "contact_id", "bot_id", "root_agent_id", "event_id"]
            data = jwt.decode(token, key, algorithms=["RS256"], issuer=self._issuer, audience=self._audience,
                              options={"require": required, "strict_aud": True})
            if expected_request_sha256 is not None:
                signed_digest = data.get("request_sha256")
                if not isinstance(signed_digest, str) or not hmac.compare_digest(signed_digest, expected_request_sha256):
                    raise InvalidGrant("invalid_grant")
            if type(data["iat"]) is not int or type(data["exp"]) is not int or not 0 < data["exp"] - data["iat"] <= 60:
                raise InvalidGrant("invalid_grant")
            scope = data["account_scope"]
            if not isinstance(scope, str) or scope != self._account_scope:
                raise InvalidGrant("invalid_grant")
            fields = ["conversation_id", "inbox_id", "contact_id", "bot_id", "root_agent_id", "event_id", "jti"]
            identifiers = {field: str(UUID(data[field])) for field in fields}
            if identifiers["root_agent_id"] != str(UUID(expected_root_agent_id)) or identifiers["conversation_id"] != str(UUID(expected_conversation_id)) or identifiers["contact_id"] != str(UUID(expected_contact_id)):
                raise InvalidGrant("invalid_grant")
            return VerifiedGrant(scope, identifiers["conversation_id"], identifiers["inbox_id"], identifiers["contact_id"], identifiers["bot_id"],
                                 identifiers["root_agent_id"], identifiers["event_id"], data["iat"], data["exp"], identifiers["jti"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
            raise InvalidGrant("invalid_grant") from None

    def verify_request(self, token: str, *, request_body: bytes, expected_root_agent_id: str,
                       expected_conversation_id: str, expected_contact_id: str) -> VerifiedGrant:
        """Verify the exact received bytes before consulting an idempotency cache.

        This alone does not authorize execution or permit an event retry; the
        caller must also use an event-bound, atomic idempotency ledger.
        """
        if not isinstance(request_body, bytes) or not request_body:
            raise InvalidGrant("invalid_grant")
        return self.verify(token, expected_root_agent_id=expected_root_agent_id,
                           expected_conversation_id=expected_conversation_id, expected_contact_id=expected_contact_id,
                           expected_request_sha256=hashlib.sha256(request_body).hexdigest())
