"""Resolve the agent platform URL for this Odoo installation."""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "agent_config.json"


def server_url(_env):
    """Read the platform URL shipped with the module."""
    config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return config["agent_server_url"].strip().rstrip("/")
