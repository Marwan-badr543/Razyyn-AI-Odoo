# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Write Guard: Safety validations, limit checks, and ledger controls for Odoo."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Optional

from odoo import fields
from odoo.exceptions import UserError, ValidationError


class AgentWriteError(Exception):
    """Base exception for all agent write operations."""
    def __init__(self, message: str, code: str = "WRITE_ERROR") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class NotAgentSessionError(AgentWriteError):
    def __init__(self, message: str = "The request did not authenticate as the agent user.") -> None:
        super().__init__(message, "NOT_AGENT_SESSION")


class WritePolicyDisabledError(AgentWriteError):
    def __init__(self, message: str = "Agent writes are disabled in Agent Write Policy.") -> None:
        super().__init__(message, "POLICY_DISABLED")


class ModelNotAllowedError(AgentWriteError):
    def __init__(self, message: str = "Writing to this model is not permitted by policy.") -> None:
        super().__init__(message, "MODEL_NOT_ALLOWED")


class DryRunOnlyError(AgentWriteError):
    def __init__(self, message: str = "Agent Write Policy is configured for Dry Run only.") -> None:
        super().__init__(message, "DRY_RUN_ONLY")


class PolicyCapExceededError(AgentWriteError):
    def __init__(self, message: str = "Transaction exceeds write policy limit.") -> None:
        super().__init__(message, "POLICY_CAP_EXCEEDED")


class AccountBlockedError(AgentWriteError):
    def __init__(self, message: str = "Modification of this account is blocked by policy.") -> None:
        super().__init__(message, "ACCOUNT_BLOCKED")


class PostingDateForbiddenError(AgentWriteError):
    def __init__(self, message: str = "Posting date violates policy limits.") -> None:
        super().__init__(message, "DATE_OUT_OF_BOUNDS")


class WriteRejectedError(AgentWriteError):
    """A write the system would not accept.

    Takes an explicit *code* because the agent turns codes into sentences for
    the customer, and "this system is in check-only mode" and "that exceeds
    your per-run limit" must not arrive as the same one.
    """

    def __init__(self, message: str = "Write operation failed validation.",
                 code: str = "WRITE_REJECTED") -> None:
        super().__init__(message, code)


class ResourceNotFoundError(AgentWriteError):
    def __init__(self, message: str = "Requested record was not found.") -> None:
        super().__init__(message, "RESOURCE_NOT_FOUND")


class MissingParameterError(AgentWriteError):
    def __init__(self, message: str = "Missing required parameter.") -> None:
        super().__init__(message, "MISSING_PARAMETER")


def check_write_policy(env, model_name: str, payload: Optional[Mapping[str, Any]] = None) -> None:
    """Enforce write policy constraints before any modification."""
    policy = env["razyyn.agent.write.policy"].sudo().get_policy_singleton()

    if not policy.enabled:
        raise WritePolicyDisabledError()

    if policy.restrict_to_listed_models:
        allowed_models = {m.model for m in policy.allowed_model_ids}
        if model_name not in allowed_models:
            raise ModelNotAllowedError(
                f"Model '{model_name}' is not in the allowed models list of Agent Write Policy."
            )

    if not payload:
        return

    # Check posting date constraints if date/invoice_date present
    posting_date_str = payload.get("date") or payload.get("invoice_date")
    if posting_date_str:
        if isinstance(posting_date_str, str):
            try:
                posting_date = datetime.strptime(posting_date_str[:10], "%Y-%m-%d").date()
            except ValueError:
                posting_date = date.today()
        elif isinstance(posting_date_str, (date, datetime)):
            posting_date = posting_date_str if isinstance(posting_date_str, date) else posting_date_str.date()
        else:
            posting_date = date.today()

        today = date.today()
        days_diff = (posting_date - today).days

        if days_diff > policy.posting_date_max_days_forward:
            raise PostingDateForbiddenError(
                f"Posting date {posting_date} is {days_diff} days in the future, "
                f"exceeding max forward limit of {policy.posting_date_max_days_forward} days."
            )
        if policy.posting_date_max_days_back > 0 and (-days_diff) > policy.posting_date_max_days_back:
            raise PostingDateForbiddenError(
                f"Posting date {posting_date} is {-days_diff} days in the past, "
                f"exceeding max backdating limit of {policy.posting_date_max_days_back} days."
            )

    # Check total amount cap
    if policy.max_total_amount_per_run > 0:
        total_amount = float(payload.get("amount") or payload.get("amount_total") or 0.0)
        # Sum invoice lines or move lines if present
        lines = payload.get("invoice_line_ids") or payload.get("line_ids") or []
        if isinstance(lines, list):
            line_sum = 0.0
            for line in lines:
                if isinstance(line, (tuple, list)) and len(line) >= 3 and isinstance(line[2], dict):
                    line_dict = line[2]
                    line_sum += float(line_dict.get("price_subtotal") or line_dict.get("debit") or line_dict.get("credit") or 0.0)
                elif isinstance(line, dict):
                    line_sum += float(line.get("price_subtotal") or line.get("debit") or line.get("credit") or 0.0)
            if line_sum > total_amount:
                total_amount = line_sum

        if total_amount > policy.max_total_amount_per_run:
            raise PolicyCapExceededError(
                f"Transaction amount {total_amount} exceeds maximum allowed per run ({policy.max_total_amount_per_run})."
            )

    # Check blocked accounts
    if policy.blocked_account_ids:
        blocked_ids = set(policy.blocked_account_ids.ids)
        # Scan lines for account_id
        lines = payload.get("invoice_line_ids") or payload.get("line_ids") or []
        if isinstance(lines, list):
            for line in lines:
                account_id = None
                if isinstance(line, (tuple, list)) and len(line) >= 3 and isinstance(line[2], dict):
                    account_id = line[2].get("account_id")
                elif isinstance(line, dict):
                    account_id = line.get("account_id")
                if account_id and int(account_id) in blocked_ids:
                    acc_name = env["account.account"].sudo().browse(int(account_id)).display_name
                    raise AccountBlockedError(
                        f"Account '{acc_name}' is in the blocked accounts list of Agent Write Policy."
                    )


def validate_ledger_move_balance(model_name: str, payload: Mapping[str, Any]) -> None:
    """Ensure debits and credits balance for journal entries."""
    if model_name != "account.move":
        return
    move_type = payload.get("move_type") or "entry"
    if move_type != "entry":
        return

    lines = payload.get("line_ids") or []
    if not lines or not isinstance(lines, list):
        return

    total_debit = 0.0
    total_credit = 0.0
    for line in lines:
        line_dict = {}
        if isinstance(line, (tuple, list)) and len(line) >= 3 and isinstance(line[2], dict):
            line_dict = line[2]
        elif isinstance(line, dict):
            line_dict = line
        total_debit += float(line_dict.get("debit") or 0.0)
        total_credit += float(line_dict.get("credit") or 0.0)

    if abs(total_debit - total_credit) > 0.001:
        raise AgentWriteError(
            f"Unbalanced journal entry: Total debits ({total_debit}) must equal total credits ({total_credit}).",
            "UNBALANCED_ENTRY",
        )

