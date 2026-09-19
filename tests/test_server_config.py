"""The platform URL is deployable without exposing a local override to Git."""

import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "razyyn_ai/services/server_config.py"
_spec = importlib.util.spec_from_file_location("server_config_under_test", _SOURCE)
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)


class ServerUrlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = pathlib.Path(self.folder.name) / "agent_config.json"
        patch = mock.patch.object(config, "_CONFIG_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)

        self.params = mock.MagicMock()
        self.params.sudo.return_value.get_param.return_value = False
        self.env = {"ir.config_parameter": self.params}

    def test_production_without_local_config(self):
        self.assertEqual(config.server_url(self.env), "https://api.razyyn.com")

    def test_local_config(self):
        self.path.write_text('{"agent_server_url": "http://localhost:8010/"}')
        self.assertEqual(config.server_url(self.env), "http://localhost:8010")

    def test_database_override_takes_precedence(self):
        self.path.write_text('{"agent_server_url": "http://localhost:8010"}')
        self.params.sudo.return_value.get_param.return_value = "https://another.example/"
        self.assertEqual(config.server_url(self.env), "https://another.example")

    def test_bad_config_falls_back_to_production(self):
        self.path.write_text("{invalid")
        self.assertEqual(config.server_url(self.env), "https://api.razyyn.com")


if __name__ == "__main__":
    unittest.main()
