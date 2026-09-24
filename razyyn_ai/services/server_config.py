"""Resolve the agent platform URL for this Odoo installation."""

import json
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1]
_LOCAL_CONFIG_PATH = _MODULE_PATH / "agent_config.json"
_DEFAULT_CONFIG_PATH = _MODULE_PATH / "agent_config.default.json"


def server_url(_env):
    """Read a private local override, or the committed production default."""
    path = _LOCAL_CONFIG_PATH if _LOCAL_CONFIG_PATH.exists() else _DEFAULT_CONFIG_PATH
    config = json.loads(path.read_text(encoding="utf-8"))
    url = config["agent_server_url"].strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid agent_server_url in {path}")
    return url
