"""Simulated local tools for the customer-support AI agent.

No real email, Slack, GitHub or payment integration — everything is local and
deterministic so the hackathon demo works offline. Each function returns a
plain dict that the forensic layer records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"


def _load(name: str) -> Any:
    with open(DATA_DIR / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_emails() -> dict[str, Any]:
    """Return the simulated support inbox."""
    return {"ok": True, "emails": _load("emails.json")}


def search_documents(query: str) -> dict[str, Any]:
    """Search the simulated document store (token-based, underscore tolerant)."""
    documents = _load("documents.json")
    tokens = [token for token in (query or "").lower().split() if len(token) > 2]
    results: list[dict[str, Any]] = []
    for key, value in documents.items():
        text = json.dumps(value, ensure_ascii=False).lower().replace("_", " ")
        key_text = key.lower().replace("_", " ")
        if tokens and all(token in text or token in key_text for token in tokens):
            results.append({"collection": key, "content": value})
    return {"ok": True, "query": query, "results": results}


def create_ticket(subject: str, description: str) -> dict[str, Any]:
    """Create a support ticket in the simulated ticket system."""
    return {
        "ok": True,
        "ticket_id": "TICKET-201",
        "subject": subject,
        "description": description,
        "status": "open",
    }


def send_external_message(to: str, body: str) -> dict[str, Any]:
    """Simulate sending a message to an external party."""
    return {"ok": True, "sent": True, "to": to, "body": body}


def delete_ticket(ticket_id: str) -> dict[str, Any]:
    """Simulate deleting a support ticket."""
    return {"ok": True, "deleted": ticket_id}


def finish(summary: str) -> dict[str, Any]:
    """Close the run with a short summary."""
    return {"ok": True, "summary": summary}


TOOL_REGISTRY = {
    "read_emails": read_emails,
    "search_documents": search_documents,
    "create_ticket": create_ticket,
    "send_external_message": send_external_message,
    "delete_ticket": delete_ticket,
    "finish": finish,
}


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a simulated tool by name. Never called for blocked calls."""
    function = TOOL_REGISTRY.get(tool_name)
    if function is None:
        return {"ok": False, "error": f"unknown tool: {tool_name}"}
    try:
        return function(**(arguments or {}))
    except TypeError as exc:
        return {"ok": False, "error": f"bad arguments: {exc}"}
    except Exception as exc:  # noqa: BLE001 - malformed input must not kill the run
        # A bad argument can reach a tool body before Python raises TypeError
        # (for example a non-string query hitting ``query.lower()``). The run
        # loop must receive a normal tool result instead of an unhandled
        # exception, otherwise the request 500s and the run stays "running".
        return {
            "ok": False,
            "error": f"invalid arguments: {type(exc).__name__}: {exc}",
        }
