"""Resolve the agent platform URL for this Odoo installation."""

import json
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)
_CONFIG_PATH = Path(__file__).resolve().parents[1] / "agent_config.json"
_DEFAULT_URL = "https://api.razyyn.com"


def server_url(env):
    """An Odoo database override takes precedence over the local config file."""
    override = env["ir.config_parameter"].sudo().get_param("razyyn_ai.server_url")
    if override and override.strip():
        return override.strip().rstrip("/")

    try:
        config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _DEFAULT_URL
    except (OSError, ValueError) as exc:
        _logger.warning("Cannot read agent config %s: %s", _CONFIG_PATH, exc)
        return _DEFAULT_URL

    url = config.get("agent_server_url") if isinstance(config, dict) else None
    if isinstance(url, str) and url.strip():
        return url.strip().rstrip("/")
    return _DEFAULT_URL
