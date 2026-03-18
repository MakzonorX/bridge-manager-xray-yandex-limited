from __future__ import annotations

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

import app.main as main
import app.stats as stats
from app.models import User
from app.storage import get_session
from tests.helpers import isolated_storage, make_settings


class ApiTrafficGuardTests(unittest.TestCase):
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
            session.add(
                User(
                    user_id=user_id,
                    uuid="11111111-1111-1111-1111-111111111111",
                    label="seed",
                    created_at=datetime.now(timezone.utc),
                    revoked_at=None,
                )
            )
            session.commit()
        finally:
            session.close()

    def test_delete_user_blocks_traffic_read_until_config_apply_finishes(self) -> None:
        self._seed_user("alice")
        stats._merge_user_snapshot("alice", 900, 900)

        mutation_entered = threading.Event()
        release_mutation = threading.Event()
        fetch_started = threading.Event()
        delete_result: dict[str, object] = {}
        traffic_result: dict[str, object] = {}

        def fake_remove_user_client(*_args, **_kwargs) -> bool:
            mutation_entered.set()
            self.assertFalse(fetch_started.is_set())
            self.assertTrue(release_mutation.wait(timeout=2))
            return True

        def fake_fetch_user_runtime_stats(*_args, **_kwargs):
            fetch_started.set()
            return stats.RuntimeSnapshot(uplink=20, downlink=20)

        with mock.patch("app.main.persist_all_user_runtime_totals"), mock.patch(
            "app.main.remove_user_client",
            side_effect=fake_remove_user_client,
        ), mock.patch(
            "app.stats.fetch_user_runtime_stats",
            side_effect=fake_fetch_user_runtime_stats,
        ):
            delete_thread = threading.Thread(
                target=lambda: delete_result.setdefault(
                    "value",
                    main.delete_user("alice", None, self.settings),
                )
            )
            traffic_thread = threading.Thread(
                target=lambda: traffic_result.setdefault(
                    "value",
                    stats.get_user_traffic(self.settings, "alice"),
                )
            )

            delete_thread.start()
            self.assertTrue(mutation_entered.wait(timeout=2))

            traffic_thread.start()
            time.sleep(0.2)
            self.assertFalse(fetch_started.is_set())

            release_mutation.set()

            delete_thread.join(timeout=2)
            traffic_thread.join(timeout=2)

        self.assertEqual(
            delete_result["value"],
            {"status": "deleted", "user_id": "alice", "removed_from_xray": True},
        )
        self.assertEqual(
            traffic_result["value"],
            {
                "uplink_bytes": 920,
                "downlink_bytes": 920,
                "runtime_uplink_bytes": 20,
                "runtime_downlink_bytes": 20,
            },
        )

    @mock.patch("app.main.get_user_traffic")
    def test_user_traffic_endpoint_keeps_compatible_response_shape(self, get_user_traffic: mock.Mock) -> None:
        self._seed_user("alice")
        get_user_traffic.return_value = {
            "uplink_bytes": 1,
            "downlink_bytes": 2,
            "runtime_uplink_bytes": 3,
            "runtime_downlink_bytes": 4,
        }

        response = main.user_traffic("alice", None, self.settings)

        self.assertEqual(
            response,
            {
                "user_id": "alice",
                "uplink_bytes": 1,
                "downlink_bytes": 2,
                "runtime_uplink_bytes": 3,
                "runtime_downlink_bytes": 4,
            },
        )
