"""The production URL ships with the module; local URLs stay local."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "razyyn_ai"
spec = importlib.util.spec_from_file_location(
    "razyyn_server_config_under_test", MODULE / "services" / "server_config.py"
)
server_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_config)


class AgentServerURL(unittest.TestCase):
    def test_a_fresh_install_uses_the_committed_production_url(self):
        with mock.patch.object(server_config, "_LOCAL_CONFIG_PATH", MODULE / "no-local-config.json"):
            self.assertEqual(server_config.server_url(None), "https://api.razyyn.com")

    def test_an_ignored_local_file_overrides_the_production_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(json.dumps({"agent_server_url": "http://localhost:8010/"}))
            with mock.patch.object(server_config, "_LOCAL_CONFIG_PATH", path):
                self.assertEqual(server_config.server_url(None), "http://localhost:8010")

    def test_an_invalid_local_url_fails_before_a_request_is_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(json.dumps({"agent_server_url": "localhost:8010"}))
            with mock.patch.object(server_config, "_LOCAL_CONFIG_PATH", path):
                with self.assertRaises(ValueError):
                    server_config.server_url(None)
