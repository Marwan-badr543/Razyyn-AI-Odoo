"""Resolve the agent platform URL for this Odoo installation."""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "agent_config.json"
_DEFAULT_URL = "https://api.razyyn.com"


def server_url(_env):
    """Use the local override, or the production URL on a fresh install."""
    try:
        config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _DEFAULT_URL
    return config["agent_server_url"].strip().rstrip("/")
