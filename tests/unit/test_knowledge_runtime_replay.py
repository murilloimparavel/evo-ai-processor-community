import threading
import unittest
import os
import sys
import time
import types
from pathlib import Path
from uuid import uuid4

package = types.ModuleType("isolated_knowledge_runtime")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime")]
sys.modules[package.__name__] = package
from isolated_knowledge_runtime.grant import InvalidGrant, VerifiedGrant
from isolated_knowledge_runtime.replay import ReplayGuard


class AtomicStore:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def set(self, name, value, *, nx, ex):
        assert nx and ex > 0
        with self.lock:
            if name in self.items:
                return None
            self.items[name] = (value, ex)
            return True


class ReplayGuardTest(unittest.TestCase):
    def setUp(self):
        self.grant = VerifiedGrant("single", "conversation", "inbox", "contact", "bot", "root", "event", 100, 160, "grant")
        self.store = AtomicStore()
        self.guard = ReplayGuard(self.store, clock=lambda: 100.5)

    def test_claim_is_atomic_under_concurrency(self):
        outcomes = []
        def claim():
            try:
                self.guard.consume(self.grant)
                outcomes.append(True)
            except InvalidGrant:
                outcomes.append(False)
        threads = [threading.Thread(target=claim) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 29)
        self.assertEqual(self.store.items["knowledge:grant:used:grant"][1], 60)

    def test_expired_grants_and_store_errors_fail_closed(self):
        with self.assertRaises(InvalidGrant):
            ReplayGuard(self.store, clock=lambda: 160).consume(self.grant)
        with self.assertRaises(InvalidGrant):
            ReplayGuard(self.store, clock=lambda: 99).consume(self.grant)
        class BrokenStore:
            def set(self, *args, **kwargs):
                raise ConnectionError("sensitive internal detail")
        with self.assertRaisesRegex(InvalidGrant, "^invalid_grant$"):
            ReplayGuard(BrokenStore(), clock=lambda: 100).consume(self.grant)

    @unittest.skipUnless(os.getenv("KNOWLEDGE_REPLAY_TEST_REDIS_URL"), "isolated Redis not configured")
    def test_real_redis_allows_only_one_simultaneous_claim(self):
        import redis
        store = redis.Redis.from_url(os.environ["KNOWLEDGE_REPLAY_TEST_REDIS_URL"])
        grant_id = str(uuid4())
        now = int(time.time())
        grant = VerifiedGrant("single", "conversation", "inbox", "contact", "bot", "root", "event", now, now + 60, grant_id)
        guard = ReplayGuard(store)
        barrier = threading.Barrier(12)
        successes = []

        def claim():
            barrier.wait()
            try:
                guard.consume(grant)
                successes.append(True)
            except InvalidGrant:
                successes.append(False)

        try:
            threads = [threading.Thread(target=claim) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(successes.count(True), 1)
            self.assertEqual(successes.count(False), 11)
            self.assertGreater(store.ttl(f"knowledge:grant:used:{grant_id}"), 0)
        finally:
            store.delete(f"knowledge:grant:used:{grant_id}")
