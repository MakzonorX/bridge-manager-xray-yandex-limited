from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
import types
from unittest import mock

sys.modules.setdefault("portalocker", types.SimpleNamespace(lock=lambda *_args, **_kwargs: None, LOCK_EX=1))

from app.models import User, UserLimitPolicy, UserTraffic
from app.storage import get_session
from tests.helpers import isolated_storage, make_settings


def _write_config_with_routing(path: Path) -> None:
    path.write_text(
        json.dumps({
            "inbounds": [
                {"tag": "inbound-from-users", "settings": {"clients": []}},
            ],
            "outbounds": [
                {"protocol": "vless", "tag": "to-exit", "settings": {"vnext": [{"address": "exit.example.com", "port": 443, "users": [{"id": "bridge-uuid", "encryption": "none"}]}]}, "streamSettings": {"network": "xhttp", "security": "tls", "xhttpSettings": {"path": "/bridge-xh"}, "tlsSettings": {"serverName": "exit.example.com"}}},
                {"protocol": "freedom", "tag": "direct", "settings": {}},
                {"protocol": "blackhole", "tag": "blocked", "settings": {}},
            ],
            "routing": {
                "rules": [
                    {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                    {"type": "field", "inboundTag": ["inbound-from-users"], "outboundTag": "to-exit"},
                    {"type": "field", "inboundTag": ["socks-test"], "outboundTag": "to-exit"},
                ]
            },
        }),
        encoding="utf-8",
    )


class LimitPolicyValidationTests(unittest.TestCase):
    """Unit tests for LimitPolicyRequest validation."""

    def test_unlimited_clears_fields(self) -> None:
        from app.main import LimitPolicyRequest
        p = LimitPolicyRequest(mode="unlimited", traffic_limit_bytes=123)
        self.assertIsNone(p.traffic_limit_bytes)
        self.assertIsNone(p.post_limit_action)
        self.assertIsNone(p.throttle_rate_bytes_per_sec)

    def test_limited_requires_traffic_limit(self) -> None:
        from app.main import LimitPolicyRequest
        with self.assertRaises(Exception):
            LimitPolicyRequest(mode="limited", post_limit_action="block")

    def test_limited_requires_positive_limit(self) -> None:
        from app.main import LimitPolicyRequest
        with self.assertRaises(Exception):
            LimitPolicyRequest(mode="limited", traffic_limit_bytes=0, post_limit_action="block")

    def test_limited_requires_post_limit_action(self) -> None:
        from app.main import LimitPolicyRequest
        with self.assertRaises(Exception):
            LimitPolicyRequest(mode="limited", traffic_limit_bytes=1000)

    def test_limited_throttle_default_rate(self) -> None:
        from app.main import LimitPolicyRequest
        p = LimitPolicyRequest(mode="limited", traffic_limit_bytes=1000, post_limit_action="throttle")
        self.assertEqual(p.throttle_rate_bytes_per_sec, 102400)

    def test_limited_throttle_custom_rate(self) -> None:
        from app.main import LimitPolicyRequest
        p = LimitPolicyRequest(mode="limited", traffic_limit_bytes=1000, post_limit_action="throttle", throttle_rate_bytes_per_sec=51200)
        self.assertEqual(p.throttle_rate_bytes_per_sec, 51200)

    def test_limited_block_no_rate(self) -> None:
        from app.main import LimitPolicyRequest
        p = LimitPolicyRequest(mode="limited", traffic_limit_bytes=1000, post_limit_action="block")
        self.assertIsNone(p.throttle_rate_bytes_per_sec)

    def test_invalid_mode_rejected(self) -> None:
        from app.main import LimitPolicyRequest
        with self.assertRaises(Exception):
            LimitPolicyRequest(mode="foobar", traffic_limit_bytes=1000)

    def test_invalid_action_rejected(self) -> None:
        from app.main import LimitPolicyRequest
        with self.assertRaises(Exception):
            LimitPolicyRequest(mode="limited", traffic_limit_bytes=1000, post_limit_action="slowdown")

    def test_negative_limit_rejected(self) -> None:
        from app.main import LimitPolicyRequest
        with self.assertRaises(Exception):
            LimitPolicyRequest(mode="limited", traffic_limit_bytes=-100, post_limit_action="block")

    def test_negative_rate_rejected(self) -> None:
        from app.main import LimitPolicyRequest
        with self.assertRaises(Exception):
            LimitPolicyRequest(mode="limited", traffic_limit_bytes=1000, post_limit_action="throttle", throttle_rate_bytes_per_sec=-1)


class EnforcementStateTransitionTests(unittest.TestCase):
    """Tests for enforcement state transitions in DB."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tempdir.name))
        self.storage_ctx = isolated_storage(self.settings)
        self.storage_ctx.__enter__()

    def tearDown(self) -> None:
        self.storage_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _seed_user(self, user_id: str) -> None:
        session = get_session()
        try:
            session.add(User(
                user_id=user_id, uuid="11111111-1111-1111-1111-111111111111",
                label="test", created_at=datetime.now(timezone.utc), revoked_at=None,
            ))
            session.commit()
        finally:
            session.close()

    def _seed_traffic(self, user_id: str, up: int, down: int) -> None:
        session = get_session()
        try:
            traffic = session.get(UserTraffic, user_id)
            if traffic is None:
                traffic = UserTraffic(
                    user_id=user_id, total_uplink=up, total_downlink=down,
                    last_runtime_uplink=0, last_runtime_downlink=0,
                    updated_at=datetime.now(timezone.utc),
                )
            else:
                traffic.total_uplink = up
                traffic.total_downlink = down
            session.add(traffic)
            session.commit()
        finally:
            session.close()

    def _seed_policy(self, user_id: str, mode: str = "limited", limit: int = 1000,
                     action: str = "throttle", state: str = "none") -> None:
        session = get_session()
        try:
            policy = UserLimitPolicy(
                user_id=user_id, mode=mode,
                traffic_limit_bytes=limit if mode == "limited" else None,
                post_limit_action=action if mode == "limited" else None,
                throttle_rate_bytes_per_sec=102400,
                enforcement_state=state,
            )
            session.add(policy)
            session.commit()
        finally:
            session.close()

    def test_unlimited_user_not_enforced(self) -> None:
        from app.enforcement import check_and_enforce_limits
        self._seed_user("alice")
        self._seed_traffic("alice", 5000, 5000)
        self._seed_policy("alice", mode="unlimited")

        changed = check_and_enforce_limits(self.settings)
        self.assertFalse(changed)

    def test_limited_below_threshold_not_enforced(self) -> None:
        from app.enforcement import check_and_enforce_limits
        self._seed_user("alice")
        self._seed_traffic("alice", 200, 200)
        self._seed_policy("alice", mode="limited", limit=1000, action="throttle")

        changed = check_and_enforce_limits(self.settings)
        self.assertFalse(changed)

    def test_limited_reaches_threshold_throttled(self) -> None:
        from app.enforcement import check_and_enforce_limits
        self._seed_user("alice")
        self._seed_traffic("alice", 600, 500)
        self._seed_policy("alice", mode="limited", limit=1000, action="throttle")

        changed = check_and_enforce_limits(self.settings)
        self.assertTrue(changed)

        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "alice")
            self.assertEqual(policy.enforcement_state, "throttled")
            self.assertIsNotNone(policy.limit_reached_at)
        finally:
            session.close()

    def test_limited_reaches_threshold_blocked(self) -> None:
        from app.enforcement import check_and_enforce_limits
        self._seed_user("alice")
        self._seed_traffic("alice", 600, 500)
        self._seed_policy("alice", mode="limited", limit=1000, action="block")

        changed = check_and_enforce_limits(self.settings)
        self.assertTrue(changed)

        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "alice")
            self.assertEqual(policy.enforcement_state, "blocked")
            self.assertIsNotNone(policy.limit_reached_at)
        finally:
            session.close()

    def test_already_throttled_not_reprocessed(self) -> None:
        from app.enforcement import check_and_enforce_limits
        self._seed_user("alice")
        self._seed_traffic("alice", 600, 500)
        self._seed_policy("alice", mode="limited", limit=1000, action="throttle", state="throttled")

        changed = check_and_enforce_limits(self.settings)
        self.assertFalse(changed)

    def test_already_blocked_not_reprocessed(self) -> None:
        from app.enforcement import check_and_enforce_limits
        self._seed_user("alice")
        self._seed_traffic("alice", 600, 500)
        self._seed_policy("alice", mode="limited", limit=1000, action="block", state="blocked")

        changed = check_and_enforce_limits(self.settings)
        self.assertFalse(changed)

    def test_unlimited_resets_enforcement(self) -> None:
        from app.enforcement import clear_enforcement
        self._seed_user("alice")
        self._seed_policy("alice", mode="limited", limit=1000, action="throttle", state="throttled")

        cleared = clear_enforcement(self.settings, "alice")
        self.assertTrue(cleared)

        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "alice")
            self.assertEqual(policy.enforcement_state, "none")
            self.assertIsNone(policy.limit_reached_at)
        finally:
            session.close()

    def test_clear_enforcement_noop_when_none(self) -> None:
        from app.enforcement import clear_enforcement
        self._seed_user("alice")
        self._seed_policy("alice", mode="unlimited")

        cleared = clear_enforcement(self.settings, "alice")
        self.assertFalse(cleared)

    def test_enforcement_persists_across_sessions(self) -> None:
        """Verify enforcement_state survives a fresh DB session."""
        from app.enforcement import check_and_enforce_limits
        self._seed_user("alice")
        self._seed_traffic("alice", 600, 500)
        self._seed_policy("alice", mode="limited", limit=1000, action="block")

        check_and_enforce_limits(self.settings)

        # Fresh session
        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "alice")
            self.assertEqual(policy.enforcement_state, "blocked")
        finally:
            session.close()


class XrayConfigEnforcementTests(unittest.TestCase):
    """Tests for Xray config generation with enforcement routing."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tempdir.name))
        _write_config_with_routing(Path(self.settings.xray_config))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _load_config(self) -> dict:
        return json.loads(Path(self.settings.xray_config).read_text(encoding="utf-8"))

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_normal_user_no_enforcement_rules(self, _val, _rest, _wait) -> None:
        from app.xray_config import apply_enforcement_routing
        result = apply_enforcement_routing(self.settings, [], [])
        # No enforcement rules, no change expected
        self.assertFalse(result)

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_throttled_user_routed_to_throttle_outbound(self, _val, _rest, _wait) -> None:
        from app.xray_config import apply_enforcement_routing, THROTTLE_OUTBOUND_TAG
        result = apply_enforcement_routing(self.settings, ["alice"], [])
        self.assertTrue(result)

        config = self._load_config()
        # Check throttle outbound exists
        throttle_obs = [ob for ob in config["outbounds"] if ob["tag"] == THROTTLE_OUTBOUND_TAG]
        self.assertEqual(len(throttle_obs), 1)
        self.assertEqual(throttle_obs[0]["streamSettings"]["sockopt"]["mark"], self.settings.limited_tc_mark)

        # Check routing rule
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 1)
        self.assertEqual(enforcement_rules[0]["user"], ["user:alice"])
        self.assertEqual(enforcement_rules[0]["outboundTag"], THROTTLE_OUTBOUND_TAG)

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_blocked_user_routed_to_blackhole(self, _val, _rest, _wait) -> None:
        from app.xray_config import apply_enforcement_routing, BLOCKED_OUTBOUND_TAG
        result = apply_enforcement_routing(self.settings, [], ["bob"])
        self.assertTrue(result)

        config = self._load_config()
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 1)
        self.assertEqual(enforcement_rules[0]["user"], ["user:bob"])
        self.assertEqual(enforcement_rules[0]["outboundTag"], BLOCKED_OUTBOUND_TAG)

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_mixed_throttle_and_block(self, _val, _rest, _wait) -> None:
        from app.xray_config import apply_enforcement_routing
        result = apply_enforcement_routing(self.settings, ["alice"], ["bob"])
        self.assertTrue(result)

        config = self._load_config()
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 2)

        blocked_rule = [r for r in enforcement_rules if r["outboundTag"] == "blocked"]
        throttled_rule = [r for r in enforcement_rules if r["outboundTag"] == "to-exit-throttled"]
        self.assertEqual(len(blocked_rule), 1)
        self.assertEqual(len(throttled_rule), 1)
        self.assertIn("user:bob", blocked_rule[0]["user"])
        self.assertIn("user:alice", throttled_rule[0]["user"])

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_enforcement_rules_before_default_route(self, _val, _rest, _wait) -> None:
        from app.xray_config import apply_enforcement_routing
        apply_enforcement_routing(self.settings, ["alice"], ["bob"])

        config = self._load_config()
        rules = config["routing"]["rules"]
        tags = [r.get("outboundTag") for r in rules]
        # api first, then enforcement rules, then default to-exit
        api_idx = tags.index("api")
        to_exit_idx = tags.index("to-exit")
        enforcement_indices = [i for i, r in enumerate(rules) if r.get("attrs", {}).get("_enforcement")]
        for ei in enforcement_indices:
            self.assertGreater(ei, api_idx)
            self.assertLess(ei, to_exit_idx)

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_clearing_enforcement_removes_rules(self, _val, _rest, _wait) -> None:
        from app.xray_config import apply_enforcement_routing
        # First add enforcement
        apply_enforcement_routing(self.settings, ["alice"], ["bob"])

        config = self._load_config()
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 2)

        # Then clear
        apply_enforcement_routing(self.settings, [], [])

        config = self._load_config()
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 0)

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_idempotent_enforcement_no_unnecessary_restart(self, _val, _rest, _wait) -> None:
        from app.xray_config import apply_enforcement_routing
        result1 = apply_enforcement_routing(self.settings, ["alice"], [])
        self.assertTrue(result1)

        result2 = apply_enforcement_routing(self.settings, ["alice"], [])
        self.assertFalse(result2)


class SmokeIntegrationTest(unittest.TestCase):
    """Integration test: create user, set policy, trigger enforcement, verify."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tempdir.name))
        self.storage_ctx = isolated_storage(self.settings)
        self.storage_ctx.__enter__()
        _write_config_with_routing(Path(self.settings.xray_config))

    def tearDown(self) -> None:
        self.storage_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _seed_user(self, user_id: str) -> None:
        session = get_session()
        try:
            session.add(User(
                user_id=user_id, uuid="22222222-2222-2222-2222-222222222222",
                label="integration", created_at=datetime.now(timezone.utc), revoked_at=None,
            ))
            session.commit()
        finally:
            session.close()

    def _seed_traffic(self, user_id: str, up: int, down: int) -> None:
        session = get_session()
        try:
            traffic = session.get(UserTraffic, user_id)
            if traffic is None:
                traffic = UserTraffic(
                    user_id=user_id, total_uplink=up, total_downlink=down,
                    last_runtime_uplink=0, last_runtime_downlink=0,
                    updated_at=datetime.now(timezone.utc),
                )
            else:
                traffic.total_uplink = up
                traffic.total_downlink = down
            session.add(traffic)
            session.commit()
        finally:
            session.close()

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_full_lifecycle_throttle(self, _val, _rest, _wait) -> None:
        from app.enforcement import check_and_enforce_limits, apply_current_enforcement, clear_enforcement, reapply_enforcement_routing

        self._seed_user("alice")

        # Step 1: Set limited policy
        session = get_session()
        try:
            policy = UserLimitPolicy(
                user_id="alice", mode="limited",
                traffic_limit_bytes=1000, post_limit_action="throttle",
                throttle_rate_bytes_per_sec=102400,
                enforcement_state="none",
            )
            session.add(policy)
            session.commit()
        finally:
            session.close()

        # Step 2: Traffic below limit - nothing happens
        self._seed_traffic("alice", 200, 200)
        changed = check_and_enforce_limits(self.settings)
        self.assertFalse(changed)

        # Step 3: Traffic crosses limit
        self._seed_traffic("alice", 600, 500)
        changed = check_and_enforce_limits(self.settings)
        self.assertTrue(changed)

        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "alice")
            self.assertEqual(policy.enforcement_state, "throttled")
        finally:
            session.close()

        # Step 4: Apply enforcement to Xray config
        apply_current_enforcement(self.settings)
        config = json.loads(Path(self.settings.xray_config).read_text(encoding="utf-8"))
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 1)
        self.assertIn("user:alice", enforcement_rules[0]["user"])
        self.assertEqual(enforcement_rules[0]["outboundTag"], "to-exit-throttled")

        # Step 5: Verify state persists
        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "alice")
            self.assertEqual(policy.enforcement_state, "throttled")
            self.assertIsNotNone(policy.last_enforced_at)
        finally:
            session.close()

        # Step 6: Clear enforcement (switch back to unlimited)
        clear_enforcement(self.settings, "alice")
        reapply_enforcement_routing(self.settings)

        config = json.loads(Path(self.settings.xray_config).read_text(encoding="utf-8"))
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 0)

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_full_lifecycle_block(self, _val, _rest, _wait) -> None:
        from app.enforcement import check_and_enforce_limits, apply_current_enforcement

        self._seed_user("bob")
        self._seed_traffic("bob", 600, 500)

        session = get_session()
        try:
            policy = UserLimitPolicy(
                user_id="bob", mode="limited",
                traffic_limit_bytes=1000, post_limit_action="block",
                throttle_rate_bytes_per_sec=102400,
                enforcement_state="none",
            )
            session.add(policy)
            session.commit()
        finally:
            session.close()

        changed = check_and_enforce_limits(self.settings)
        self.assertTrue(changed)

        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "bob")
            self.assertEqual(policy.enforcement_state, "blocked")
        finally:
            session.close()

        apply_current_enforcement(self.settings)
        config = json.loads(Path(self.settings.xray_config).read_text(encoding="utf-8"))
        enforcement_rules = [r for r in config["routing"]["rules"] if r.get("attrs", {}).get("_enforcement")]
        self.assertEqual(len(enforcement_rules), 1)
        self.assertIn("user:bob", enforcement_rules[0]["user"])
        self.assertEqual(enforcement_rules[0]["outboundTag"], "blocked")

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_enforcement_survives_xray_restart_simulation(self, _val, _rest, _wait) -> None:
        """Simulate: enforcement applied, xray 'restarts' (config reloaded), state still in DB."""
        from app.enforcement import check_and_enforce_limits, apply_current_enforcement

        self._seed_user("charlie")
        self._seed_traffic("charlie", 1000, 1000)

        session = get_session()
        try:
            policy = UserLimitPolicy(
                user_id="charlie", mode="limited",
                traffic_limit_bytes=1500, post_limit_action="throttle",
                throttle_rate_bytes_per_sec=102400,
                enforcement_state="none",
            )
            session.add(policy)
            session.commit()
        finally:
            session.close()

        check_and_enforce_limits(self.settings)
        apply_current_enforcement(self.settings)

        # Simulate bridge-manager restart: fresh session reads from same DB
        session = get_session()
        try:
            policy = session.get(UserLimitPolicy, "charlie")
            self.assertEqual(policy.enforcement_state, "throttled")
        finally:
            session.close()

        # Re-run enforcement loop - should NOT change anything (already throttled)
        changed = check_and_enforce_limits(self.settings)
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
