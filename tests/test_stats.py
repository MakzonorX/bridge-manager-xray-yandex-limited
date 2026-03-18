from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import UserTraffic
from app.stats import (
    RuntimeSnapshot,
    _merge_user_snapshot,
    fetch_all_user_runtime_stats,
    fetch_user_runtime_stats,
    get_user_traffic,
    persist_all_user_runtime_totals,
)
from app.storage import get_session
from tests.helpers import isolated_storage, make_settings


class TrafficAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tempdir.name))
        self.storage_ctx = isolated_storage(self.settings)
        self.storage_ctx.__enter__()

    def tearDown(self) -> None:
        self.storage_ctx.__exit__(None, None, None)
        self.tempdir.cleanup()

    def test_repeated_identical_snapshot_does_not_increase_totals(self) -> None:
        totals = _merge_user_snapshot("alice", 100, 200)
        self.assertEqual(totals, (100, 200))

        totals = _merge_user_snapshot("alice", 100, 200)
        self.assertEqual(totals, (100, 200))

    def test_runtime_reset_counts_only_current_counter(self) -> None:
        _merge_user_snapshot("alice", 900, 900)

        totals = _merge_user_snapshot("alice", 20, 20)

        self.assertEqual(totals, (920, 920))

    def test_partial_snapshot_does_not_corrupt_missing_direction_baseline(self) -> None:
        _merge_user_snapshot("alice", 900, 900)

        totals = _merge_user_snapshot("alice", 910, None)
        self.assertEqual(totals, (910, 900))

        session = get_session()
        try:
            traffic = session.get(UserTraffic, "alice")
            assert traffic is not None
            self.assertEqual(traffic.last_runtime_uplink, 910)
            self.assertEqual(traffic.last_runtime_downlink, 900)
            self.assertEqual(traffic.total_uplink, 910)
            self.assertEqual(traffic.total_downlink, 900)
        finally:
            session.close()

        totals = _merge_user_snapshot("alice", 920, 920)
        self.assertEqual(totals, (920, 920))

    @mock.patch("app.stats._run_xray_api_json")
    def test_fetch_user_runtime_stats_keeps_missing_direction_unknown(self, run_xray_api_json: mock.Mock) -> None:
        run_xray_api_json.side_effect = [
            {"stat": {"value": 900}},
            None,
        ]

        snapshot = fetch_user_runtime_stats(self.settings, "alice")

        self.assertEqual(snapshot, RuntimeSnapshot(uplink=900, downlink=None))

    @mock.patch("app.stats._run_xray_api_json")
    def test_fetch_all_user_runtime_stats_keeps_partial_query_unknown(self, run_xray_api_json: mock.Mock) -> None:
        run_xray_api_json.return_value = {
            "stat": [
                {"name": "user>>>user:alice>>>traffic>>>uplink", "value": 910},
            ]
        }

        snapshots = fetch_all_user_runtime_stats(self.settings)

        self.assertEqual(snapshots["alice"], RuntimeSnapshot(uplink=910, downlink=None))

    @mock.patch("app.stats.fetch_all_user_runtime_stats")
    def test_collector_merges_only_reliable_direction(self, fetch_all_runtime_stats: mock.Mock) -> None:
        _merge_user_snapshot("alice", 900, 900)
        fetch_all_runtime_stats.return_value = {
            "alice": RuntimeSnapshot(uplink=None, downlink=930),
        }

        persist_all_user_runtime_totals(self.settings)

        session = get_session()
        try:
            traffic = session.get(UserTraffic, "alice")
            assert traffic is not None
            self.assertEqual(traffic.total_uplink, 900)
            self.assertEqual(traffic.last_runtime_uplink, 900)
            self.assertEqual(traffic.total_downlink, 930)
            self.assertEqual(traffic.last_runtime_downlink, 930)
        finally:
            session.close()

    @mock.patch("app.stats.fetch_user_runtime_stats")
    def test_get_user_traffic_preserves_contract_shape_on_partial_snapshot(self, fetch_runtime_stats: mock.Mock) -> None:
        _merge_user_snapshot("alice", 900, 900)
        fetch_runtime_stats.return_value = RuntimeSnapshot(uplink=920, downlink=None)

        traffic = get_user_traffic(self.settings, "alice")

        self.assertEqual(
            traffic,
            {
                "uplink_bytes": 920,
                "downlink_bytes": 900,
                "runtime_uplink_bytes": 920,
                "runtime_downlink_bytes": 0,
            },
        )
