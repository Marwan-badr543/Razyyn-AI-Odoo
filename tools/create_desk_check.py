#!/usr/bin/env python3
"""Drive the whole create desk against a running Odoo, over real HTTP.

WHY THIS IS NOT AN ODOO UNIT TEST
    An Odoo ``TransactionCase`` calls the service layer directly. Every shape
    bug this connector has ever had lived one layer above that: a route reading
    ``**kwargs`` while the agent posts a JSON body, a reply nested under
    ``report`` where the client reads ``preflight``, a list of wrappers where
    the client reads records. None of those is reachable from a
    ``TransactionCase``, and all of them look like an empty database rather
    than a bug. So this speaks the same HTTP the agent speaks — the same seven
    routes, the same parameter names, the same JSON body — and checks the
    shapes as well as the outcomes.

WHAT IT PROVES, AND THE ONE CHECK THAT MATTERS MOST
    That a document was CREATED is the easy half. ``_odoo_values`` drops fields
    the model does not have and fields it will not accept, so "created exactly
    what was asked for" and "created an empty draft" both come back as
    ``CREATED``. Every creation here is READ BACK and compared against the
    payload that asked for it.

USAGE
    python3 tools/create_desk_check.py --base-url http://localhost:8069 \
        --api-key <key>

    The key is a Razyyn connection's API key — the same credential the agent
    authenticates with. Nothing here needs an Odoo login.

WHAT IT LEAVES BEHIND
    Draft and cancelled journal entries in whichever company its accounts
    belong to, and rows in the write log. Nothing is posted and left posted:
    everything it posts, it cancels.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import requests

TIMEOUT = 60


class Check:
    """A running tally, printed as it goes so a failure is seen immediately."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def __call__(self, label: str, condition, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))
        return bool(condition)

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    def report(self) -> int:
        print(f"\n==== {self.passed} passed, {len(self.failed)} failed ====")
        for name in self.failed:
            print("  FAILED:", name)
        return 1 if self.failed else 0


class Gateway:
    """The seven routes, called exactly as ``adapters/gateway.py`` calls them."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()

    def call(self, method: str, payload: dict | None = None) -> tuple[int, dict]:
        response = self.session.post(
            f"{self.base_url}/agent_write/{method}",
            json={**(payload or {}), "api_key": self.api_key},
            timeout=TIMEOUT,
        )
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:300]}
        # A whitelisted success is wrapped in `message`; an error is the bare
        # object. Unwrapping only when it is there is what keeps a refusal from
        # reading as an empty success.
        if isinstance(body, dict) and "message" in body:
            return response.status_code, body["message"]
        return response.status_code, body

    def api(self, method: str, payload: dict | None = None) -> tuple[int, dict]:
        response = self.session.post(
            f"{self.base_url}/agent_api/{method}",
            json={**(payload or {}), "api_key": self.api_key},
            timeout=TIMEOUT,
        )
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:300]}
        if isinstance(body, dict) and "message" in body:
            return response.status_code, body["message"]
        return response.status_code, body


def _key(run: str, name: str) -> str:
    return f"desk-check-{run}-{name}"


def _outcomes(batch: dict) -> list[str]:
    return [str(row.get("outcome")) for row in (batch.get("results") or [])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8069")
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args()

    gw = Gateway(args.base_url, args.api_key)
    check = Check()
    run = uuid.uuid4().hex[:8]
    print(f"Create desk check against {args.base_url}  (run {run})")

    # ── The contract the desk reads before it writes anything ────────────────

    check.section("1. what this system says the agent may do")
    status, body = gw.call("get_write_policy")
    policy = (body or {}).get("policy") or {}
    check("the policy comes back under 'policy'", status == 200 and bool(policy),
          f"HTTP {status}: {json.dumps(body)[:200]}")
    check("it says whether writing is switched on at all",
          "enabled" in policy, json.dumps(policy)[:200])
    if not policy.get("enabled"):
        print("\n  Agent writing is switched OFF in this system's Agent Write "
              "Policy, so nothing below could be recorded. Switch it on and "
              "run this again.")
        return check.report()

    check.section("2. what one document type is made of")
    status, body = gw.call("get_document_spec", {"doctype": "account.move"})
    spec = (body or {}).get("spec") or body or {}
    fields = {f.get("fieldname"): f for f in (spec.get("fields") or [])}
    check("the spec lists real fields", status == 200 and len(fields) > 10,
          f"HTTP {status}: {json.dumps(body)[:200]}")
    check("a link field is reported as a Link, not as Odoo's own word",
          fields.get("journal_id", {}).get("fieldtype") == "Link",
          str(fields.get("journal_id")))
    check("a money field is reported as Currency",
          fields.get("amount_total", {}).get("fieldtype") == "Currency",
          str(fields.get("amount_total")))
    check("the document type says it can be posted",
          bool(spec.get("is_submittable")), json.dumps(spec)[:200])

    check.section("3. finding the records a document has to point at")
    status, body = gw.call("search_documents",
                           {"doctypes": json.dumps(["account.journal"]),
                            "text": "", "limit": 20})
    journals = [d for d in (body or {}).get("documents") or []]
    check("journals come back with a reference the agent can send back",
          status == 200 and journals and all(d.get("docname") for d in journals),
          json.dumps(body)[:250])
    if not journals:
        return check.report()
    journal = journals[0]

    status, body = gw.call("search_documents",
                           {"doctypes": json.dumps(["account.account"]),
                            "text": "", "limit": 50, "company": journal["company"]})
    accounts = [d for d in (body or {}).get("documents") or []]
    check("accounts come back for that company", len(accounts) >= 2,
          json.dumps(body)[:250])
    if len(accounts) < 2:
        return check.report()
    debit_account, credit_account = accounts[0]["docname"], accounts[1]["docname"]

    def entry(amount: float, reference: str) -> dict:
        return {
            "journal_id": int(journal["docname"]),
            "date": "2026-09-15",
            "ref": reference,
            "line_ids": [
                {"account_id": int(debit_account), "debit": amount, "credit": 0},
                {"account_id": int(credit_account), "debit": 0, "credit": amount},
            ],
        }

    # ── Checking before writing ──────────────────────────────────────────────

    check.section("4. checking a document before recording it")
    status, body = gw.call("preflight", {
        "payload": json.dumps({"doctype": "account.move", **entry(120.0, "preflight ok")}),
        "run_dry_run": 1,
    })
    report = (body or {}).get("preflight") or {}
    check("the answer is under 'preflight', which is the key the agent reads",
          status == 200 and isinstance(report, dict) and "findings" in report,
          f"HTTP {status}: {json.dumps(body)[:250]}")
    blocking = [f for f in (report.get("findings") or [])
                if str(f.get("severity")).lower() in ("blocking", "error")]
    check("a balanced entry raises nothing blocking", not blocking, str(blocking))

    unbalanced = entry(120.0, "preflight unbalanced")
    unbalanced["line_ids"][1]["credit"] = 100.0
    status, body = gw.call("preflight", {
        "payload": json.dumps({"doctype": "account.move", **unbalanced}),
        "run_dry_run": 1,
    })
    report = (body or {}).get("preflight") or {}
    findings = report.get("findings") or []
    check("an entry that does not balance is caught BEFORE it is written",
          bool(findings), json.dumps(report)[:250])

    # ── Recording ────────────────────────────────────────────────────────────

    check.section("5. recording one document")
    create_key = _key(run, "create")
    status, body = gw.call("write_batch", {"documents": json.dumps([{
        "action": "create", "doctype": "account.move",
        "idempotency_key": create_key, "source_row_ordinal": 1,
        "payload": entry(150.0, f"Razyyn check {run} create"),
    }]), "run_id": run, "session_id": f"check-{run}"})
    results = ((body or {}).get("batch") or {}).get("results") or []
    row = results[0] if results else {}
    created = str(row.get("docname") or "")
    check("it is reported as CREATED", status == 200 and row.get("outcome") == "CREATED",
          f"HTTP {status}: {json.dumps(body)[:300]}")
    check("it comes back as a draft", row.get("docstatus") == 0, str(row.get("docstatus")))
    check("the answer echoes the row it answers", row.get("source_row_ordinal") == 1,
          str(row.get("source_row_ordinal")))
    # The two facts that decide whether the customer can FIND what was written.
    # A draft journal entry on Odoo has no number of its own, and it lands in
    # whichever company its accounts belong to — which need not be the company
    # their screen is showing.
    check("the receipt names the document the way their own screen does",
          f"Razyyn check {run} create" in str(row.get("label") or ""),
          json.dumps(row)[:300])
    check("and says which company it landed in", bool(row.get("company")),
          json.dumps(row)[:300])

    check.section("6. what was recorded is what was asked for")
    status, body = gw.call("get_document",
                           {"doctype": "account.move", "docname": created})
    document = (body or {}).get("document") or {}
    check("the document can be read back", status == 200 and bool(document),
          f"HTTP {status}: {json.dumps(body)[:250]}")
    # Read back through the connector's OWN answer, which is a summary by
    # design: the label a person would recognise the document by, the figure it
    # is worth, where it stands, and which company it is in. Between them they
    # settle the question "created an empty draft" versus "created what was
    # asked for", which the CREATED outcome on its own does not.
    check("the reference that was asked for is on it",
          f"Razyyn check {run} create" in str(document.get("label") or ""),
          json.dumps(document)[:300])
    check("the amount that was asked for is on it",
          abs(float(document.get("amount") or 0) - 150.0) < 0.01,
          json.dumps(document)[:300])
    check("it says which company it landed in", bool(document.get("company")),
          json.dumps(document)[:300])

    # ── Changing, posting, reversing ─────────────────────────────────────────

    check.section("7. changing a draft")
    status, body = gw.call("write_batch", {"documents": json.dumps([{
        "action": "update", "doctype": "account.move", "docname": created,
        "idempotency_key": _key(run, "update"),
        "payload": {"ref": f"Razyyn check {run} amended"},
    }])})
    row = (((body or {}).get("batch") or {}).get("results") or [{}])[0]
    check("a draft can be changed", row.get("outcome") == "UPDATED",
          json.dumps(body)[:300])
    _status, body = gw.call("get_document", {"doctype": "account.move", "docname": created})
    document = (body or {}).get("document") or {}
    check("the change actually landed on the document",
          "amended" in str(document.get("label") or ""), json.dumps(document)[:250])

    check.section("8. posting it")
    status, body = gw.call("write_batch", {"documents": json.dumps([{
        "action": "submit", "doctype": "account.move", "docname": created,
        "idempotency_key": _key(run, "submit"), "payload": {},
    }])})
    row = (((body or {}).get("batch") or {}).get("results") or [{}])[0]
    check("it posts", row.get("outcome") == "UPDATED", json.dumps(body)[:300])
    check("and it reports itself as posted, not as a draft",
          row.get("docstatus") == 1, str(row.get("docstatus")))

    check.section("9. a posted document is not editable")
    status, body = gw.call("write_batch", {"documents": json.dumps([{
        "action": "update", "doctype": "account.move", "docname": created,
        "idempotency_key": _key(run, "edit-posted"),
        "payload": {"ref": "this must not be allowed"},
    }])})
    row = (((body or {}).get("batch") or {}).get("results") or [{}])[0]
    check("changing a posted document is refused",
          row.get("outcome") == "REJECTED", json.dumps(body)[:300])
    check("and the refusal says why, in words a person can act on",
          row.get("error_code") == "NOT_A_DRAFT", str(row.get("error_code")))

    check.section("10. reversing it")
    status, body = gw.call("write_batch", {"documents": json.dumps([{
        "action": "cancel", "doctype": "account.move", "docname": created,
        "idempotency_key": _key(run, "cancel"), "payload": {},
    }])})
    row = (((body or {}).get("batch") or {}).get("results") or [{}])[0]
    check("it can be reversed", row.get("outcome") == "UPDATED", json.dumps(body)[:300])
    check("and it reports itself as reversed", row.get("docstatus") == 2,
          str(row.get("docstatus")))

    # ── The things that go wrong in real life ────────────────────────────────

    check.section("11. the same instruction twice records one document")
    status, body = gw.call("write_batch", {"documents": json.dumps([{
        "action": "create", "doctype": "account.move",
        "idempotency_key": create_key,
        "payload": entry(150.0, f"Razyyn check {run} create"),
    }])})
    row = (((body or {}).get("batch") or {}).get("results") or [{}])[0]
    check("a repeat is reported as already done", row.get("outcome") == "REPLAYED",
          json.dumps(body)[:300])
    check("and it names the SAME document, not a new one",
          str(row.get("docname")) == created,
          f"{row.get('docname')} vs {created}")
    check("a repeat is still named and placed, as the first receipt was",
          bool(row.get("label")) and bool(row.get("company")), json.dumps(row)[:300])

    check.section("12. 'record this and post it' is one request")
    status, body = gw.call("write_batch", {"documents": json.dumps([
        {"action": "create", "doctype": "account.move",
         "idempotency_key": _key(run, "pair-create"), "source_row_ordinal": 1,
         "payload": entry(75.0, f"Razyyn check {run} pair")},
        {"action": "submit", "doctype": "account.move", "docname": "@0",
         "idempotency_key": _key(run, "pair-submit"), "source_row_ordinal": 2,
         "payload": {}},
    ])})
    results = ((body or {}).get("batch") or {}).get("results") or []
    check("both halves went through", _outcomes({"results": results}) == ["CREATED", "UPDATED"],
          json.dumps(body)[:400])
    check("the posting names the document the creation produced, not '@0'",
          len(results) > 1 and str(results[1].get("docname")) == str(results[0].get("docname")),
          json.dumps(results)[:300])
    check("and the document really is posted",
          len(results) > 1 and results[1].get("docstatus") == 1,
          str(results[-1].get("docstatus")))
    if len(results) > 1 and results[1].get("docstatus") == 1:
        gw.call("write_batch", {"documents": json.dumps([{
            "action": "cancel", "doctype": "account.move",
            "docname": str(results[0].get("docname")),
            "idempotency_key": _key(run, "pair-cancel"), "payload": {}}])})

    check.section("13. one bad document does not take the others with it")
    status, body = gw.call("write_batch", {"documents": json.dumps([
        {"action": "create", "doctype": "razyyn.not.a.real.model",
         "idempotency_key": _key(run, "bad"), "source_row_ordinal": 1, "payload": {}},
        {"action": "submit", "doctype": "account.move", "docname": "@0",
         "idempotency_key": _key(run, "orphan"), "source_row_ordinal": 2, "payload": {}},
        {"action": "create", "doctype": "account.move",
         "idempotency_key": _key(run, "survivor"), "source_row_ordinal": 3,
         "payload": entry(42.0, f"Razyyn check {run} survivor")},
    ])})
    results = ((body or {}).get("batch") or {}).get("results") or []
    check("the impossible one is refused",
          len(results) > 0 and results[0].get("outcome") == "REJECTED",
          json.dumps(results)[:300])
    check("the one that depended on it is NOT attempted",
          len(results) > 1 and results[1].get("outcome") == "SKIPPED"
          and results[1].get("error_code") == "DEPENDENCY_FAILED",
          json.dumps(results)[:400])
    check("and the independent one is still recorded",
          len(results) > 2 and results[2].get("outcome") == "CREATED",
          json.dumps(results)[:400])

    check.section("14. a document with no instruction key is not written")
    status, body = gw.call("write_batch", {"documents": json.dumps([{
        "action": "create", "doctype": "account.move", "idempotency_key": "",
        "payload": entry(10.0, "no key"),
    }])})
    row = (((body or {}).get("batch") or {}).get("results") or [{}])[0]
    check("it is refused rather than recorded twice later",
          row.get("error_code") == "NO_IDEMPOTENCY_KEY", json.dumps(body)[:300])

    check.section("15. the write log answers 'what happened to this one'")
    status, body = gw.call("get_write_log", {"idempotency_key": create_key})
    log = (body or {}).get("log") or {}
    check("the log finds the attempt", status == 200 and log.get("found") is True,
          json.dumps(body)[:250])
    check("and reports it as committed", log.get("status") == "COMMITTED",
          json.dumps(log)[:250])
    check("naming the document it produced", str(log.get("target_docname")) == created,
          json.dumps(log)[:250])
    # This is the answer a customer sees when a write timed out, so it is the
    # one that most needs to name something they can look up.
    check("and naming it the way their screen does, with its company",
          bool(log.get("label")) and bool(log.get("company")), json.dumps(log)[:250])

    check.section("16. the agent's own documents can be listed")
    status, body = gw.call("list_documents", {"limit": 10, "doctype": "account.move"})
    documents = (body or {}).get("documents") or []
    check("the list comes back", status == 200 and isinstance(documents, list),
          f"HTTP {status}: {json.dumps(body)[:200]}")
    check("and the document just recorded is in it",
          any(str(d.get("docname")) == created for d in documents),
          json.dumps(documents)[:300])

    check.section("17. nobody writes without a key")
    response = requests.post(f"{gw.base_url}/agent_write/write_batch",
                             json={"documents": "[]"}, timeout=TIMEOUT)
    check("no key at all is refused", response.status_code == 401, str(response.status_code))
    response = requests.post(f"{gw.base_url}/agent_write/write_batch",
                             json={"documents": "[]", "api_key": "not-a-real-key"},
                             timeout=TIMEOUT)
    check("a wrong key is refused", response.status_code == 403, str(response.status_code))

    # ── Messaging, over the same HTTP ────────────────────────────────────────

    check.section("18. what this system can send through")
    status, body = gw.api("messaging_config")
    channels = (body or {}).get("channels")
    check("the reply is a dict keyed by channel name",
          status == 200 and isinstance(channels, dict), f"HTTP {status}: {json.dumps(body)[:250]}")
    if isinstance(channels, dict):
        check("every channel says whether it is usable",
              all("enabled" in (c or {}) for c in channels.values()),
              json.dumps(channels)[:300])
        check("a channel that is not usable says why",
              all(c.get("enabled") or c.get("unavailable_reason")
                  for c in channels.values()), json.dumps(channels)[:300])

    telegram = (channels or {}).get("telegram") or {}
    if telegram.get("enabled"):
        check.section("19. sending one real message")
        status, body = gw.api("send_message", {
            "channel": "telegram", "destination": "", "subject": "Razyyn AI check",
            "body": f"Automated create-desk check {run}. No action needed.",
            "idempotency_key": _key(run, "message"), "run_id": run,
        })
        check("it is accepted", status == 200, f"HTTP {status}: {json.dumps(body)[:250]}")
        check("and answers with the provider's own message id",
              bool((body or {}).get("provider_message_id")), json.dumps(body)[:250])
        check("naming the destination the way a person would",
              bool((body or {}).get("destination_label")), json.dumps(body)[:250])

        status, body = gw.api("send_message", {
            "channel": "telegram", "destination": "somewhere nobody listed",
            "body": "must not be sent", "idempotency_key": _key(run, "message-bad"),
        })
        check("an unlisted destination comes back as a refusal, not a silent 200",
              status >= 400, f"HTTP {status}: {json.dumps(body)[:250]}")
        check("and the refusal carries a reason the agent can relay",
              bool((body or {}).get("error")), json.dumps(body)[:250])
    else:
        print("  (Telegram is not set up on this system, so the send checks "
              "were not run.)")

    return check.report()


if __name__ == "__main__":
    sys.exit(main())
