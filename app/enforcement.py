from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .models import UserLimitPolicy, UserTraffic
from .settings import Settings
from .storage import get_session
from .xray_config import XrayConfigError, apply_enforcement_routing

LOG = logging.getLogger(__name__)


def get_or_create_policy(user_id: str) -> UserLimitPolicy:
    session = get_session()
    try:
        policy = session.get(UserLimitPolicy, user_id)
        if policy is None:
            policy = UserLimitPolicy(
                user_id=user_id,
                mode="unlimited",
                enforcement_state="none",
                throttle_rate_bytes_per_sec=102400,
            )
            session.add(policy)
            session.commit()
            session.refresh(policy)
        return policy
    finally:
        session.close()


def _collect_enforcement_lists(session) -> tuple[list[str], list[str]]:
    policies = session.query(UserLimitPolicy).filter(
        UserLimitPolicy.enforcement_state.in_(["throttled", "blocked"])
    ).all()
    throttled = [p.user_id for p in policies if p.enforcement_state == "throttled"]
    blocked = [p.user_id for p in policies if p.enforcement_state == "blocked"]
    return throttled, blocked


def check_and_enforce_limits(settings: Settings) -> bool:
    session = get_session()
    changed = False
    try:
        policies = session.query(UserLimitPolicy).filter(
            UserLimitPolicy.mode == "limited",
            UserLimitPolicy.enforcement_state == "none",
        ).all()

        for policy in policies:
            traffic = session.get(UserTraffic, policy.user_id)
            if traffic is None:
                continue

            total_bytes = traffic.total_uplink + traffic.total_downlink
            if policy.traffic_limit_bytes is not None and total_bytes >= policy.traffic_limit_bytes:
                now = datetime.now(timezone.utc)
                if policy.post_limit_action == "block":
                    policy.enforcement_state = "blocked"
                    LOG.info("limit_reached user_id=%s total_bytes=%d limit=%d action=block",
                             policy.user_id, total_bytes, policy.traffic_limit_bytes)
                elif policy.post_limit_action == "throttle":
                    policy.enforcement_state = "throttled"
                    LOG.info("limit_reached user_id=%s total_bytes=%d limit=%d action=throttle",
                             policy.user_id, total_bytes, policy.traffic_limit_bytes)
                else:
                    continue

                policy.limit_reached_at = now
                policy.last_enforced_at = now
                session.add(policy)
                changed = True

        if changed:
            session.commit()
        return changed
    finally:
        session.close()


def apply_current_enforcement(settings: Settings) -> bool:
    session = get_session()
    try:
        throttled, blocked = _collect_enforcement_lists(session)
    finally:
        session.close()

    if not throttled and not blocked:
        return False

    try:
        LOG.info("xray_reload_started throttled=%s blocked=%s", throttled, blocked)
        result = apply_enforcement_routing(settings, throttled, blocked)
        if result:
            LOG.info("xray_reload_succeeded")
            session2 = get_session()
            try:
                now = datetime.now(timezone.utc)
                for uid in throttled:
                    p = session2.get(UserLimitPolicy, uid)
                    if p:
                        p.last_enforced_at = now
                        session2.add(p)
                        LOG.info("throttle_applied user_id=%s", uid)
                for uid in blocked:
                    p = session2.get(UserLimitPolicy, uid)
                    if p:
                        p.last_enforced_at = now
                        session2.add(p)
                        LOG.info("block_applied user_id=%s", uid)
                session2.commit()
            finally:
                session2.close()
        return result
    except XrayConfigError as exc:
        LOG.error("xray_reload_failed error=%s", exc)
        return False


def clear_enforcement(settings: Settings, user_id: str) -> bool:
    session = get_session()
    try:
        policy = session.get(UserLimitPolicy, user_id)
        if policy is None or policy.enforcement_state == "none":
            return False

        old_state = policy.enforcement_state
        policy.enforcement_state = "none"
        policy.limit_reached_at = None
        policy.last_enforced_at = None
        session.add(policy)
        session.commit()
        LOG.info("enforcement_cleared user_id=%s old_state=%s", user_id, old_state)
        return True
    finally:
        session.close()


def reapply_enforcement_routing(settings: Settings) -> bool:
    session = get_session()
    try:
        throttled, blocked = _collect_enforcement_lists(session)
    finally:
        session.close()

    try:
        LOG.info("xray_reload_started throttled=%s blocked=%s", throttled, blocked)
        result = apply_enforcement_routing(settings, throttled, blocked)
        if result:
            LOG.info("xray_reload_succeeded")
        return result
    except XrayConfigError as exc:
        LOG.error("xray_reload_failed error=%s", exc)
        return False


class EnforcementLoop:
    def __init__(self, settings: Settings, interval_seconds: int = 15) -> None:
        self.settings = settings
        self.interval_seconds = max(interval_seconds, 5)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="enforcement-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                changed = check_and_enforce_limits(self.settings)
                if changed:
                    apply_current_enforcement(self.settings)
            except Exception as exc:
                LOG.warning("enforcement loop iteration failed: %s", exc)
