# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""1-Click Self-Service Connection: Link this Odoo ERP to the Razyyn Agent platform.

Mirrors Frappe connect.py:
- Ensures dedicated system user (razyyn_ai@razyyn.internal)
- Mints API credentials
- Registers connection with platform (/api/create/connections) with erp_code="ODOO"
- Automatically enables write policy on connect
- Handles key rotation, disconnect, and recording toggle
"""

from __future__ import annotations

import logging
import requests
from typing import Any, Optional

from odoo import fields
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

#: The dedicated Odoo user the agent acts as. Named for the product, so an
#: administrator reading their user list sees who it is.
AGENT_USER_LOGIN = "razyyn_ai@razyyn.internal"
AGENT_USER_NAME = "Razyyn AI Agent"

#: What it used to be called. Kept so an install that already created the user
#: under the old login finds THAT one and carries on, instead of quietly
#: creating a second agent user beside it — two accounts with the same
#: permissions, one of them holding the connection the customer actually
#: authorised, and no way for them to tell which.
_LEGACY_AGENT_USER_LOGINS = ("accountant_agent@razyyn.internal",)


def _find_agent_user(env):
    """The agent's Odoo user, under its current login or a previous one."""
    users = env["res.users"].sudo()
    for login in (AGENT_USER_LOGIN, *_LEGACY_AGENT_USER_LOGINS):
        found = users.search([("login", "=", login)], limit=1)
        if found:
            return found
    return users.browse()
_PLATFORM_TIMEOUT_SECONDS = 45


def _server_url(env) -> str:
    param = env["ir.config_parameter"].sudo().get_param(
        "razyyn_ai.server_url", "http://localhost:8010"
    )
    return param.rstrip("/")


def _public_site_url(env) -> str:
    override = env["ir.config_parameter"].sudo().get_param("razyyn_ai.public_url")
    if override:
        return override.rstrip("/")
    base_url = env["ir.config_parameter"].sudo().get_param("web.base.url", "http://localhost:8069")
    return base_url.rstrip("/")


def ensure_agent_user(env) -> models.Model:
    """Ensure the dedicated system user for Razyyn Agent exists and is active."""
    user = _find_agent_user(env)
    if not user:
        # Create user
        groups = [
            env.ref("base.group_user").id,
            env.ref("razyyn_ai.group_razyyn_user").id,
            env.ref("razyyn_ai.group_razyyn_manager").id,
        ]
        # Try to add accounting group if exists
        try:
            acc_group = env.ref("account.group_account_user")
            if acc_group:
                groups.append(acc_group.id)
        except Exception:
            pass

        user = env["res.users"].sudo().create({
            "name": AGENT_USER_NAME,
            "login": AGENT_USER_LOGIN,
            "email": AGENT_USER_LOGIN,
            "active": True,
            "groups_id": [(6, 0, groups)],
        })
    elif not user.active:
        user.sudo().write({"active": True})

    return user


#: The one record holding the key the platform calls this Odoo back with.
PLATFORM_CONNECTION_LABEL = "Razyyn Platform Connection"


def ensure_agent_credentials(env, force_new: bool = False) -> tuple[models.Model, str]:
    """The agent's own key for this Odoo, issued on ONE record.

    A new key replaces the old one on the same connection rather than adding a
    second connection. Pressing Connect four times used to leave four records
    behind, and each of their keys still opened the write API -- so the
    customer had four live credentials for an integration they believe has one,
    and revoking the one they can see revokes nothing.
    """
    settings_model = env["razyyn.agent.settings"].sudo()
    existing = settings_model.search(
        [("label", "=", PLATFORM_CONNECTION_LABEL)], order="id asc",
    )

    if not existing:
        return settings_model.generate(label=PLATFORM_CONNECTION_LABEL)

    record = existing[0]
    if len(existing) > 1:
        # Left over from when connecting created a record each time. They are
        # working credentials nobody is watching, so they go.
        _logger.info("Razyyn AI: retiring %s duplicate connection records",
                     len(existing) - 1)
        (existing - record).unlink()

    if not force_new:
        return record, ""
    return record, record.reissue_api_key()


def platform_request(
    env, method: str, path: str, access_token: str, data: Optional[dict] = None
) -> dict:
    """Send authenticated request to Razyyn platform."""
    url = f"{_server_url(env)}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.request(
            method, url, data=data, headers=headers, timeout=_PLATFORM_TIMEOUT_SECONDS
        )
        if response.status_code >= 400:
            detail = response.text
            try:
                b = response.json()
                detail = b.get("detail") or detail
            except Exception:
                pass
            raise UserError(f"Razyyn Platform Error ({response.status_code}): {detail}")
        return response.json() or {}
    except requests.exceptions.RequestException as exc:
        _logger.error("Razyyn platform request failed: %s", exc)
        raise UserError(f"Could not reach Razyyn platform: {exc}")


def get_write_connection_status(env, user_id: int) -> dict[str, Any]:
    """Read connection status from this Odoo instance."""
    agent_user = _find_agent_user(env)
    user_settings = env["razyyn.agent.settings"].sudo().search([("user_id", "=", user_id)], limit=1)
    if not user_settings:
        user_settings = env["razyyn.agent.settings"].sudo().search([], limit=1)

    policy = env["razyyn.agent.write.policy"].sudo().get_policy_singleton()

    agent_user_exists = bool(agent_user)
    agent_user_enabled = bool(agent_user and agent_user.active)
    connected_to_platform = bool(user_settings and user_settings.erp_connection_id)
    recording_enabled = bool(user_settings and user_settings.write_recording_enabled)

    missing = []
    if not agent_user_exists or not agent_user_enabled:
        missing.append("Agent system user is not provisioned. Click Connect to provision it.")
    if not connected_to_platform:
        missing.append("Click Connect to link this Odoo instance to your Razyyn account.")
    elif not recording_enabled:
        missing.append("Recording is off. Enable Allow Agent Recording when you are ready.")
    if not policy.enabled:
        missing.append("Agent Write Policy is disabled. Enable it in Governance & Settings.")

    return {
        "agent_user": AGENT_USER_LOGIN,
        "agent_user_exists": agent_user_exists,
        "agent_user_enabled": agent_user_enabled,
        "has_credentials": bool(user_settings and user_settings.api_key_hash),
        "connected_to_platform": connected_to_platform,
        "recording_enabled": recording_enabled,
        "connected_on": user_settings.write_connected_on.isoformat() if user_settings and user_settings.write_connected_on else None,
        "last_error": user_settings.write_last_error if user_settings else None,
        "policy_enabled": bool(policy.enabled),
        "dry_run_only": bool(policy.dry_run_only),
        "allowed_models": [m.model for m in policy.allowed_model_ids],
        "missing": missing,
    }


def connect_write_access(env, user_id: int, enable_recording: bool = False) -> dict[str, Any]:
    """1-Click Connect handshake."""
    # ONE PLACE HOLDS THIS USER'S SESSION, and it is razyyn.agent.settings.
    # There used to be a second record with the same two tokens in it, and the
    # two disagreed: signing in through the chat wrote one, connecting for
    # recording read the other, and a customer who was plainly signed in was
    # told to sign in first.
    user_settings = env["razyyn.agent.settings"].sudo().search([("user_id", "=", user_id)], limit=1)
    if not user_settings or not user_settings.access_token:
        raise UserError("Please sign in to Razyyn from the chat interface first.")

    # Provision user and key
    agent_user = ensure_agent_user(env)
    conn_rec, plaintext_key = ensure_agent_credentials(env, force_new=False)
    if not plaintext_key:
        conn_rec, plaintext_key = ensure_agent_credentials(env, force_new=True)

    # COMMITTED BEFORE THE PLATFORM IS TOLD ABOUT IT, AND THAT IS THE WHOLE
    # POINT OF THIS LINE.
    #
    # Registering a connection makes the platform turn round and call this Odoo
    # back to check the credentials work. That call arrives as a SEPARATE
    # request, in a separate transaction, which cannot see a key this one has
    # not committed yet. So the key was always rejected -- every single
    # connection came back "your ERP rejected the agent's credentials", and the
    # advice it gave was to press Connect again, which failed the same way.
    env.cr.commit()

    site_url = _public_site_url(env)

    # Register on platform
    result = platform_request(
        env, "POST", "/api/create/connections", user_settings.access_token,
        data={
            "site_url": site_url,
            "api_key": plaintext_key,
            "api_secret": plaintext_key,  # Odoo uses single secret API key
            "agent_erp_user": AGENT_USER_LOGIN,
            "label": env.company.name or "Odoo",
            "erp_code": "ODOO",
        },
    )

    connection_id = result.get("erp_connection_id")
    verified = bool(result.get("verified"))
    recording = bool(result.get("write_enabled"))

    # Update local record
    user_settings.write({
        "erp_connection_id": connection_id,
        "agent_erp_user_id": agent_user.id,
        "write_connected_on": fields.Datetime.now() if verified else False,
        "write_last_error": None if verified else result.get("verification_error"),
    })

    # Enable write policy locally
    policy = env["razyyn.agent.write.policy"].sudo().get_policy_singleton()
    if not policy.enabled:
        policy.write({"enabled": True})

    # ASKED FOR, SO ACTUALLY DONE.
    #
    # `enable_recording` used to be accepted and then ignored: the connection
    # was made, recording stayed off on the platform, and the next thing the
    # customer saw was the agent telling them it was "not connected to an
    # accounting system I can write to" -- about the system they had just
    # connected. Registering a connection deliberately never turns recording on
    # by itself, so if it was asked for, it is a second call and it is made here.
    if enable_recording and connection_id and verified and not recording:
        try:
            recording = bool(set_recording_enabled(env, user_id, True).get("recording_enabled"))
        except UserError as refused:
            _logger.warning("Razyyn AI: recording could not be turned on: %s", refused)

    user_settings.write({"write_recording_enabled": recording})

    return {
        "connected": bool(connection_id),
        "verified": verified,
        "site_url": site_url,
        "agent_user": AGENT_USER_LOGIN,
        "verification_error": result.get("verification_error"),
        "recording_enabled": recording,
        # SAYS WHAT ACTUALLY HAPPENED. This used to read "Connected. Your Odoo
        # ERP and Razyyn AI can now communicate seamlessly." whatever the
        # outcome -- including over a verification failure whose own message,
        # returned in the same breath, said the credentials had been rejected.
        "message": _connection_message(verified, recording, result.get("verification_error")),
    }


def _connection_message(verified: bool, recording: bool, error: Optional[str]) -> str:
    """One sentence a person can act on, matching what really happened."""
    if not verified:
        return (
            "Connected, but Razyyn AI could not yet reach this Odoo with the "
            "credentials it was given: " + (error or "the check did not pass.")
        )
    if not recording:
        return (
            "Connected. Razyyn AI can read this Odoo. Recording of entries is "
            "still off -- turn it on when you want the agent to prepare documents."
        )
    return "Connected. Razyyn AI can read this Odoo and record entries, subject to your Write Policy."


def rotate_agent_credentials(env, user_id: int) -> dict[str, Any]:
    """Issue a fresh key for this Odoo and tell the platform about it.

    The button on the settings form has always offered this and there has never
    been a function behind it -- pressing it raised an AttributeError, which
    Odoo shows as "Something went wrong". A customer who believed their key was
    compromised, pressed the button meant for exactly that, and was shown an
    error, still had the old key live.

    Rotating is one operation, not two: the new key is issued on the same
    record (so the old one stops working) and re-registered in the same call
    (so the platform is never holding a credential this Odoo has retired).
    """
    user_settings = env["razyyn.agent.settings"].sudo().search(
        [("user_id", "=", user_id)], limit=1
    )
    if not user_settings or not user_settings.access_token:
        raise UserError("Please sign in to Razyyn from the chat interface first.")

    _record, plaintext_key = ensure_agent_credentials(env, force_new=True)
    # Committed before the platform is told, for the same reason connecting is:
    # verification arrives as a separate request that cannot see an uncommitted
    # key, and would report the fresh credentials as rejected.
    env.cr.commit()

    result = platform_request(
        env, "POST", "/api/create/connections", user_settings.access_token,
        data={
            "site_url": _public_site_url(env),
            "api_key": plaintext_key,
            "api_secret": plaintext_key,
            "agent_erp_user": AGENT_USER_LOGIN,
            "label": env.company.name or "Odoo",
            "erp_code": "ODOO",
        },
    )

    verified = bool(result.get("verified"))
    user_settings.write({
        "erp_connection_id": result.get("erp_connection_id"),
        "write_connected_on": fields.Datetime.now() if verified else False,
        "write_last_error": None if verified else result.get("verification_error"),
    })
    return {
        "verified": verified,
        "message": (
            "New credentials issued and registered. The previous key no longer works."
            if verified else
            "New credentials were issued, but Razyyn AI could not verify them: "
            + (result.get("verification_error") or "the check did not pass.")
        ),
    }


def set_recording_enabled(env, user_id: int, enabled: bool) -> dict[str, Any]:
    """Toggle recording on the platform."""
    user_settings = env["razyyn.agent.settings"].sudo().search([("user_id", "=", user_id)], limit=1)
    if not user_settings or not user_settings.erp_connection_id:
        raise UserError("Please connect this Odoo instance to Razyyn first.")

    result = platform_request(
        env, "POST", f"/api/create/connections/{user_settings.erp_connection_id}/write-enabled",
        user_settings.access_token,
        data={"enabled": "true" if enabled else "false"},
    )
    recording = bool(result.get("write_enabled"))
    user_settings.write({"write_recording_enabled": recording})
    return {
        "recording_enabled": recording,
        "message": "Recording enabled. The agent may now record entries subject to Write Policy." if recording else "Recording disabled.",
    }


def disconnect_write_access(env, user_id: int) -> dict[str, Any]:
    """Disconnect ERP from the platform."""
    user_settings = env["razyyn.agent.settings"].sudo().search([("user_id", "=", user_id)], limit=1)
    if user_settings and user_settings.erp_connection_id:
        try:
            platform_request(
                env, "DELETE", f"/api/create/connections/{user_settings.erp_connection_id}",
                user_settings.access_token,
            )
        except Exception as e:
            _logger.warning("Failed to delete platform connection: %s", e)

        user_settings.write({
            "erp_connection_id": False,
            "write_recording_enabled": False,
            "write_connected_on": False,
            "write_last_error": False,
        })

    return {"connected": False, "message": "Disconnected from Razyyn AI platform."}
