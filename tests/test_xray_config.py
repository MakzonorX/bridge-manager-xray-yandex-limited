from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
import sys
import types
from unittest import mock

sys.modules.setdefault("portalocker", types.SimpleNamespace(lock=lambda *_args, **_kwargs: None, LOCK_EX=1))

from app.xray_config import XrayConfigError, _wait_for_xray_api_ready, upsert_user_client
from tests.helpers import make_settings


def _write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "inbounds": [
                    {
                        "tag": "inbound-from-users",
                        "settings": {"clients": []},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


class XrayConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tempdir.name))
        _write_config(Path(self.settings.xray_config))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @mock.patch("app.xray_config.time.sleep")
    @mock.patch("app.xray_config.subprocess.run")
    def test_wait_for_xray_api_ready_retries_until_probe_succeeds(
        self,
        subprocess_run: mock.Mock,
        _sleep: mock.Mock,
    ) -> None:
        subprocess_run.side_effect = [
            CompletedProcess(args=["xray"], returncode=1, stdout="", stderr="not ready"),
            CompletedProcess(args=["xray"], returncode=0, stdout='{"stat":[]}', stderr=""),
        ]

        _wait_for_xray_api_ready(self.settings, ready_timeout=1.0, interval_seconds=0)

        self.assertEqual(subprocess_run.call_count, 2)

    @mock.patch("app.xray_config._wait_for_xray_api_ready")
    @mock.patch("app.xray_config._restart_xray")
    @mock.patch("app.xray_config._validate_xray_config")
    def test_upsert_user_client_rolls_back_config_when_readiness_fails(
        self,
        _validate_xray_config: mock.Mock,
        restart_xray: mock.Mock,
        wait_for_xray_api_ready: mock.Mock,
    ) -> None:
        original_config = Path(self.settings.xray_config).read_text(encoding="utf-8")
        wait_for_xray_api_ready.side_effect = XrayConfigError("not ready")

        with self.assertRaises(XrayConfigError):
            upsert_user_client(self.settings, "alice", "22222222-2222-2222-2222-222222222222")

        self.assertEqual(Path(self.settings.xray_config).read_text(encoding="utf-8"), original_config)
        self.assertEqual(restart_xray.call_count, 2)
