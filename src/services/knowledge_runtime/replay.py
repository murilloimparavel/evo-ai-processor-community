"""Atomic one-use claims for verified grants, independent of ADK history.

This does not establish a session, tenant, delegation or binding. Call only
after grant verification and immediately before the protected operation.
The caller must separately coordinate normal A2A idempotent retries; a reused
grant is always denied, including when the previous operation later fails.
"""

import time
from typing import Callable, Protocol

from .grant import InvalidGrant, VerifiedGrant


class AtomicStore(Protocol):
    def set(self, name: str, value: str, *, nx: bool, ex: int) -> object: ...


class ReplayGuard:
    def __init__(self, store: AtomicStore, *, clock: Callable[[], float] = time.time):
        self._store = store
        self._clock = clock

    def consume(self, grant: VerifiedGrant) -> None:
        # Redis expires whole seconds; round up so the claim never disappears
        # while the signed grant is valid. The verifier caps lifetime at 60 s.
        remaining = grant.expires_at - self._clock()
        if not 0 < remaining <= 60:
            raise InvalidGrant("invalid_grant")
        ttl = int(remaining) + 1
        try:
            claimed = self._store.set(f"knowledge:grant:used:{grant.grant_id}", "1", nx=True, ex=ttl)
        except Exception:
            raise InvalidGrant("invalid_grant") from None
        if not claimed:
            raise InvalidGrant("invalid_grant")
