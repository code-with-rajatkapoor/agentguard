"""AgentGuard API server.

FastAPI + CORS. Serves the dashboard at `/` and exposes:

    POST /api/run-agent   -> run normal | attack | ai
    POST /api/approve     -> approve / block a pending action
    GET  /api/forensics   -> every forensic event on disk
    GET  /api/health      -> status + configured model

Runs are resumable: when the guard returns REQUIRE_APPROVAL the run pauses,
its session is kept in memory, and POST /api/approve continues it.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from agent import ATTACK_PLAN, NORMAL_PLAN, AIAgent
from forensic import read_events, record_event, verify_chain
from guard import inspect_tool_call
from models import AgentRunRequest, ApproveRequest, RedTeamRequest, RunResponse, StopRequest
from providers import (
    PROVIDER_SPECS,
    ProviderConfigError,
    describe_provider,
    resolve_provider,
    verify_provider,
)
from redteam import ATTACK_TYPES, generate_attack
from tools import execute_tool

load_dotenv()

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"
DEFAULT_TASK = "Summarize today's support emails and create a ticket for the billing complaint."
MAX_ITERATIONS = 20

app = FastAPI(
    title="AgentGuard",
    description="Runtime firewall and forensic recorder for AI agents",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory resumable run sessions, keyed by run_id.
RUNS: dict[str, dict[str, Any]] = {}

# Upper bound on retained sessions. Only finished sessions are evicted, so a
# running run or one waiting on an approval is never dropped.
MAX_RUNS = 100


def _evict_finished_runs() -> None:
    """Keep RUNS bounded without stranding live or pending sessions."""
    if len(RUNS) <= MAX_RUNS:
        return
    finished = [
        run_id
        for run_id, item in RUNS.items()
        if item.get("status") in ("completed", "blocked")
        and not item.get("pending")
        and not item.get("warm")
    ]
    for run_id in finished[: len(RUNS) - MAX_RUNS]:
        RUNS.pop(run_id, None)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _new_session(task: str, mode: str, provider: str | None = None) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    session: dict[str, Any] = {
        "run_id": run_id,
        "task": task or DEFAULT_TASK,
        "mode": mode,
        "agent_name": "support-agent",
        "untrusted": [],
        "history": [],
        "ai_agent": None,
        "plan": [],
        "index": 0,
        "iterations": 0,
        "tool_calls": [],
        "forensics": [],
        "blocked_actions": [],
        "pending": None,
        "status": "running",
        "agent_output": "",
        "fallback": False,
        # Set by POST /api/stop; checked by the run loop each iteration.
        "stop_requested": False,
        # Serialises approve requests for this session so the same pending
        # action cannot be executed twice.
        "_lock": threading.Lock(),
        # LIVE_AI | SCRIPTED | SCRIPTED_FALLBACK
        "run_mode": "LIVE_AI" if mode == "ai" else "SCRIPTED",
        "fallback_reason": None,
        "provider": None,
        "model": None,
    }

    if mode == "attack":
        session["plan"] = ATTACK_PLAN
    elif mode == "ai":
        try:
            config = resolve_provider(provider)
        except ProviderConfigError as exc:
            # No usable provider: start in scripted fallback so the demo still
            # works, and surface the exact reason instead of failing silently.
            config = None
            session["fallback"] = True
            session["run_mode"] = "SCRIPTED_FALLBACK"
            session["fallback_reason"] = str(exc)
            session["plan"] = NORMAL_PLAN
        if config is not None:
            session["ai_agent"] = AIAgent(session["task"], config)
            session["provider"] = config.provider
            session["model"] = config.model
    elif mode == "redteam":
        session["plan"] = []  # the caller injects the generated attack plan
    else:
        session["mode"] = "normal"
        session["plan"] = NORMAL_PLAN

    RUNS[run_id] = session
    _evict_finished_runs()
    return session


def _context(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": session["run_id"],
        "agent_name": session["agent_name"],
        "user_task": session["task"],
        "mode": session["mode"],
        "untrusted_content_seen": session["untrusted"],
        "tool_history": session["history"],
    }


def _next_call(session: dict[str, Any]) -> dict[str, Any] | None:
    """Ask the agent for its next proposed tool call."""
    if session["mode"] == "ai" and not session["fallback"]:
        call = session["ai_agent"].next_call()
        if call is not None:
            # The agent records its own assistant turn in its history.
            return call
        if session["ai_agent"].fallback:
            # Provider failed / returned invalid JSON twice -> scripted fallback.
            session["fallback"] = True
            session["run_mode"] = "SCRIPTED_FALLBACK"
            session["fallback_reason"] = session["ai_agent"].fallback_reason
            session["plan"] = NORMAL_PLAN
            session["index"] = 0
        else:
            return None

    if session["index"] < len(session["plan"]):
        call = session["plan"][session["index"]]
        session["index"] += 1
        return call
    return None


def _collect_untrusted(tool_name: str, result: dict[str, Any], session: dict[str, Any]) -> None:
    """Remember content the agent saw so the guard can inspect it."""
    if not isinstance(result, dict):
        return
    if tool_name == "read_emails":
        for email in result.get("emails", []):
            body = str(email.get("body", ""))
            sender = str(email.get("from", ""))
            if body:
                session["untrusted"].append(f"[email from {sender}] {body}")
    elif tool_name == "search_documents":
        for item in result.get("results", []):
            collection = item.get("collection", "")
            content = json.dumps(item.get("content", ""), ensure_ascii=False)
            session["untrusted"].append(f"[document:{collection}] {content}")


def _record_tool_result(session: dict[str, Any], call: dict[str, Any], result: dict[str, Any]) -> None:
    """Feed an executed tool result back into the AI conversation (if any)."""

    agent_obj = session.get("ai_agent")
    if session["mode"] == "ai" and agent_obj is not None:
        agent_obj.record_tool_result(call, result)


def _record_block(session: dict[str, Any], call: dict[str, Any], reason: str) -> None:
    """Tell the AI model that a proposed call was blocked."""

    agent_obj = session.get("ai_agent")
    if session["mode"] == "ai" and agent_obj is not None:
        agent_obj.record_block(call, reason)


def _record_approval_block(session: dict[str, Any], call: dict[str, Any], reason: str) -> None:
    """Tell the AI model that a human rejected its proposed call."""

    agent_obj = session.get("ai_agent")
    if session["mode"] == "ai" and agent_obj is not None:
        agent_obj.record_approval_block(call, reason)


def _record(session: dict[str, Any], event: dict[str, Any]) -> None:
    """Persist a forensic event unless this is a cache-warming run.

    Warm-up runs exercise the real agent loop to prime the provider cache;
    they must not pollute the forensic timeline the dashboard shows.
    """
    if not session.get("warm"):
        record_event(event)


def _response(session: dict[str, Any]) -> dict[str, Any]:
    return RunResponse(
        run_id=session["run_id"],
        status=session["status"],
        agent_output=session["agent_output"],
        tool_calls=session["tool_calls"],
        forensics=session["forensics"],
        blocked_action=session["blocked_actions"][0] if session["blocked_actions"] else {},
        blocked_actions=session["blocked_actions"],
        approval_request=session["pending"] or {},
        fallback=session["fallback"],
        mode=session["mode"],
        task=session["task"],
        run_mode=session.get("run_mode", "SCRIPTED"),
        fallback_reason=session.get("fallback_reason"),
        provider=session.get("provider"),
        model=session.get("model"),
    ).model_dump()


# ---------------------------------------------------------------------------
# Run engine
# ---------------------------------------------------------------------------

def _run(session: dict[str, Any]) -> dict[str, Any]:
    while True:
        # POST /api/stop asks the loop to stop; honour it before the next
        # iteration so a long-running loop cannot overwrite the blocked status.
        if session.get("stop_requested"):
            session["status"] = "blocked"
            session["pending"] = None
            if not session.get("agent_output"):
                session["agent_output"] = "Agent stopped by operator."
            return _response(session)

        session["iterations"] += 1
        if session["iterations"] > MAX_ITERATIONS:
            session["status"] = "blocked" if session["blocked_actions"] else "completed"
            if session["fallback"] and not session["agent_output"]:
                reason = session.get("fallback_reason") or "provider unavailable"
                session["agent_output"] = (
                    f"AI provider unavailable ({reason}); "
                    "finished with the scripted agent."
                )
            return _response(session)

        call = _next_call(session)
        if call is None:
            session["status"] = "blocked" if session["blocked_actions"] else "completed"
            if session["fallback"] and not session["agent_output"]:
                reason = session.get("fallback_reason") or "provider unavailable"
                session["agent_output"] = (
                    f"AI provider unavailable ({reason}); "
                    "finished with the scripted agent."
                )
            return _response(session)

        tool_name = call["tool"]
        arguments = call.get("arguments", {}) or {}
        decision = inspect_tool_call(tool_name, arguments, _context(session))
        event = dict(decision.forensic_event)

        # --- REQUIRE_APPROVAL: pause and wait for a human ------------------
        if decision.decision == "REQUIRE_APPROVAL":
            session["pending"] = {
                "event_id": event["event_id"],
                "tool": tool_name,
                "arguments": arguments,
                "tool_call_id": call.get("tool_call_id"),
                "risk_score": decision.risk_score,
                "reasons": decision.reasons,
                "matched_rules": decision.matched_rules,
            }
            session["status"] = "approval_required"
            session["forensics"].append(event)
            # Persist the pending event so it shows up in the forensic timeline
            # and the approval queue immediately (resolution appends a second
            # record with the execution result / block reason).
            _record(session, event)
            return _response(session)

        # --- BLOCK: record and keep going so every violation is visible ----
        if decision.decision == "BLOCK":
            event["block_reason"] = "; ".join(decision.reasons)
            session["forensics"].append(event)
            _record(session, event)
            session["blocked_actions"].append(
                {
                    "event_id": event["event_id"],
                    "tool": tool_name,
                    "arguments": arguments,
                    "risk_score": decision.risk_score,
                    "reasons": decision.reasons,
                    "matched_rules": decision.matched_rules,
                }
            )
            session["history"].append(tool_name)
            _record_block(session, call, "; ".join(decision.reasons))
            continue

        # --- ALLOW: execute the simulated tool ----------------------------
        result = execute_tool(tool_name, arguments)
        event["execution_result"] = result
        session["forensics"].append(event)
        _record(session, event)
        session["tool_calls"].append(
            {
                "tool": tool_name,
                "arguments": arguments,
                "decision": decision.decision,
                "risk_score": decision.risk_score,
                "result": result,
            }
        )
        session["history"].append(tool_name)
        _collect_untrusted(tool_name, result, session)
        _record_tool_result(session, call, result)

        if tool_name == "finish":
            session["agent_output"] = str(result.get("summary", "")) or "Task finished."
            session["status"] = "blocked" if session["blocked_actions"] else "completed"
            return _response(session)


def _approve(session: dict[str, Any], event_id: str, approved: bool) -> dict[str, Any]:
    """Approve or reject a pending action.

    The session lock makes the check-then-execute sequence atomic: two
    concurrent approvals for the same pending action would otherwise both pass
    the pending check and execute the tool twice.
    """
    with session["_lock"]:
        return _approve_locked(session, event_id, approved)


def _approve_locked(session: dict[str, Any], event_id: str, approved: bool) -> dict[str, Any]:
    pending = session.get("pending")
    if not pending or pending.get("event_id") != event_id:
        raise HTTPException(status_code=400, detail="No pending approval matches that event_id.")

    event = next((item for item in session["forensics"] if item.get("event_id") == event_id), None)
    if event is None:
        raise HTTPException(status_code=400, detail="Forensic event not found.")

    if approved:
        result = execute_tool(pending["tool"], pending["arguments"])
        event["execution_result"] = result
        _record(session, event)
        session["tool_calls"].append(
            {
                "tool": pending["tool"],
                "arguments": pending["arguments"],
                "decision": "ALLOW",
                "risk_score": pending["risk_score"],
                "result": result,
            }
        )
        session["history"].append(pending["tool"])
        _collect_untrusted(pending["tool"], result, session)
        _record_tool_result(session, pending, result)
    else:
        event["block_reason"] = "Blocked by human reviewer."
        _record(session, event)
        session["blocked_actions"].append(
            {
                "event_id": event_id,
                "tool": pending["tool"],
                "arguments": pending["arguments"],
                "risk_score": pending["risk_score"],
                "reasons": ["Blocked by human reviewer."],
                "matched_rules": pending.get("matched_rules", []),
            }
        )
        session["history"].append(pending["tool"])
        _record_approval_block(session, pending, "Blocked by human reviewer.")

    session["pending"] = None

    if approved and pending["tool"] == "finish":
        result = event.get("execution_result") or {}
        session["agent_output"] = str(result.get("summary", "")) or "Task finished."
        session["status"] = "blocked" if session["blocked_actions"] else "completed"
        return _response(session)

    return _run(session)


def _warm_cache(task: str) -> None:
    """Prime the provider cache for one task.

    Runs the real AI agent end-to-end with forensics disabled, so the first
    user run of the same task is served entirely from cache. Best-effort:
    any failure (provider down, unconfigured) is swallowed - warming must
    never block startup or break a run.
    """
    session: dict[str, Any] | None = None
    try:
        session = _new_session(task, "ai")
        session["warm"] = True
        _run(session)
    except Exception:
        pass
    finally:
        # Warm-up sessions are only there to prime the provider cache; they
        # must not sit in RUNS forever.
        if session is not None:
            RUNS.pop(session["run_id"], None)


@app.on_event("startup")
def _startup_warm_cache() -> None:
    """Opt-in cache pre-warm (AI_WARM=1) for the default demo task."""
    if os.getenv("AI_WARM", "0").strip().lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=_warm_cache, args=(DEFAULT_TASK,), daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def serve_frontend():
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return {"message": "AgentGuard frontend not found; open frontend/index.html manually."}


@app.get("/{filename}")
def serve_frontend_asset(filename: str):
    """Serve static frontend assets (styles.css, scripts) from frontend/."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="Not Found")
    path = FRONTEND_DIR / filename
    if path.exists() and path.is_file():
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="Not Found")


@app.get("/api/health")
def health():
    info = describe_provider()
    return {
        "status": "ok",
        "model": info.get("model") or os.getenv("NOUS_MODEL", "meituan/longcat-2.5:free"),
        "provider": info.get("provider"),
        "provider_known": info.get("known", False),
        "provider_configured": info.get("configured", False),
        # Not probed here - call /api/provider/verify for a live check.
        "provider_verified": None,
        "vercel": bool(os.environ.get("VERCEL")),
        # Relative location only: the absolute server path is not exposed.
        "frontend_index": "frontend/index.html",
        "frontend_index_exists": FRONTEND_INDEX.exists(),
        "frontend_dir_exists": FRONTEND_DIR.exists(),
    }


@app.get("/api/provider/verify")
def provider_verify():
    """Probe the configured provider with a minimal request.

    This makes a real network call and is therefore opt-in (the frontend calls
    it on demand rather than on every health poll). Never returns a key.
    """
    return verify_provider()


@app.get("/api/provider/config")
def provider_config():
    """Secret-free description of every known provider and the active one."""
    active = describe_provider()
    return {
        "active": active,
        "providers": {
            name: {
                "key_env": spec.key_env,
                "base_url": spec.base_url,
                "default_model": spec.default_model,
                "supports_native_tools": spec.supports_native_tools,
                "notes": spec.notes,
            }
            for name, spec in sorted(PROVIDER_SPECS.items())
        },
    }


@app.post("/api/run-agent")
def run_agent(request: AgentRunRequest):
    mode = (request.mode or "normal").lower()
    if mode not in ("normal", "attack", "ai"):
        raise HTTPException(status_code=400, detail="mode must be one of: normal, attack, ai")
    provider = (request.provider or "").strip().lower() or None
    if provider is not None and provider not in PROVIDER_SPECS:
        raise HTTPException(
            status_code=400,
            detail="provider must be one of: " + ", ".join(sorted(PROVIDER_SPECS)),
        )
    session = _new_session(request.task, mode, provider)
    return _run(session)


@app.post("/api/approve")
def approve(request: ApproveRequest):
    session = RUNS.get(request.run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="run_id not found")
    return _approve(session, request.event_id, request.approved)


@app.get("/api/forensics")
def forensics():
    events = read_events()
    return {"events": events, "count": len(events)}


@app.get("/api/verify")
def verify():
    """Re-check the hash chain over the forensic log."""
    return verify_chain()


@app.post("/api/red-team")
def red_team(request: RedTeamRequest):
    """Generate a randomized attack scenario and run it through AgentGuard."""
    attack_type = (request.attack_type or "").lower()
    if attack_type not in ATTACK_TYPES:
        raise HTTPException(
            status_code=400,
            detail="attack_type must be one of: " + ", ".join(ATTACK_TYPES),
        )
    attack = generate_attack(attack_type)
    session = _new_session(attack["task"], "redteam")
    session["plan"] = attack["plan"]
    result = _run(session)
    result["attack_type"] = attack["attack_type"]
    result["attack_name"] = attack["name"]
    return result


@app.post("/api/stop")
def stop_agent(request: StopRequest):
    """Halt a run (Stop Agent / Block Agent).

    Sets the stop flag so an in-flight loop notices it on its next iteration,
    and resolves any pending approval so it does not linger in the approval
    queue as a phantom entry. A run that already finished keeps the status it
    earned; only an active run ("running"/"approval_required") is relabelled
    "blocked", otherwise a completed run would be reported as blocked.
    """
    session = RUNS.get(request.run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="run_id not found")

    session["stop_requested"] = True

    pending = session.get("pending")
    if pending:
        event = next(
            (item for item in session["forensics"] if item.get("event_id") == pending.get("event_id")),
            None,
        )
        if event is not None:
            event["block_reason"] = "Blocked by operator (run stopped)."
            _record(session, event)
        session["blocked_actions"].append(
            {
                "event_id": pending.get("event_id"),
                "tool": pending.get("tool"),
                "arguments": pending.get("arguments"),
                "risk_score": pending.get("risk_score"),
                "reasons": ["Blocked by operator (run stopped)."],
                "matched_rules": pending.get("matched_rules", []),
            }
        )

    session["pending"] = None
    if session.get("status") in ("running", "approval_required"):
        session["status"] = "blocked"
        if not session.get("agent_output"):
            session["agent_output"] = "Agent stopped by operator."
    return _response(session)
