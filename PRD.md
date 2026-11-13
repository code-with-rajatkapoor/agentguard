# AgentGuard — Product Requirements Document (PRD)

**Project:** AgentGuard — runtime firewall and forensic recorder for AI agents
**Status of this document:** judge-facing snapshot of the current project
**Date:** 2026-11-13

---

## 1. Product overview

### 1.1 One-paragraph explanation
AgentGuard is a runtime firewall for AI agents. It sits between the agent and its tools, scores every proposed tool call before it executes, and decides **ALLOW**, **REQUIRE_APPROVAL**, or **BLOCK** using a deterministic policy engine. Every decision is written to an append-only forensic log (hash-chained JSONL), giving the system a complete, tamper-evident evidence trail.

### 1.2 Problem statement
AI agents are handed real powers — reading inboxes, searching document stores, creating tickets, sending messages, deleting records. That power is exactly what makes them dangerous. A single poisoned email or document can hijack an agent's context and turn it into an exfiltration engine (indirect prompt injection). Traditional security lives at the perimeter; AgentGuard lives **inside the agent loop**, at the exact moment the agent decides to act.

### 1.3 Target users and their needs
| User | Need |
|---|---|
| Developer building agents | Stop destructive/malicious tool calls before they run; get an auditable record of what the agent tried to do |
| Security operator | See every decision, every block, every approval; replay events from `logs/forensics.jsonl` |
| Hackathon judge | Clear, demoable security story with a live threat demonstration and a forensic timeline |

### 1.4 Value proposition (no exaggerated claims)
- Deterministic, policy-based defense (not a model claim — regex + rule engine).
- Pre-execution inspection: a blocked call never executes.
- Human-in-the-loop approval for anything in the 25–79 risk band.
- Built-in forensic timeline for post-incident review.
- Everything runs locally; no paid APIs, no cloud required, no external services.

---

## 2. Goals and non-goals

**Goals**
1. Inspect every tool call before execution (implemented, tested).
2. Deterministic risk scoring with three verdict bands (implemented, tested).
3. Human approval for risky calls (implemented as endpoint + demo UI; scriptable demo modes do not reach it — see Limitations).
4. Append-only, hash-chained forensic log (implemented, tested).
5. Live demo: safe run, attack run, approval, forensic timeline (implemented, demo-verified).

**Non-goals**
- Production agent firewall (documented security limitations apply).
- Real tool integrations (email/Slack/GitHub billing) — simulated tools only.
- Authentication/authorization on the API (open by design for the local demo).
- Google Cloud / cloud-hosting compliance certifications.

---

## 3. Primary user journeys

### 3.1 Safe agent run
1. Open **Run Agent**, choose **Safe Demo**.
2. Click **Run Agent**.
3. Agent reads the inbox and creates a billing ticket through allowed tools.
4. Every call is `ALLOW` (risk 0–20); run ends `completed`; green "No threats detected" banner.
5. Timeline shows every decision with risk score, reasons, and result.

### 3.2 Risky / attack run
1. Choose **Attack Demo**, click **Run Agent**.
2. Agent reads the poisoned email, searches customer records (`search_documents` → `ALLOW`, 20).
3. `send_external_message` to `attacker@example.com` → `BLOCK` (100: denied tool + external transfer + sensitive payload + suspicious sequence).
4. `delete_ticket("TICKET-101")` → `BLOCK` (100: denied tool + destructive action).
5. Run ends `blocked`; full breakdown in blocked-action cards and the timeline; injection indicators recovered from the poisoned email.

### 3.3 Human approval where supported
1. A call lands in the 25–79 band → run pauses, dashboard shows the approval panel with the tool, arguments, risk, and reasons.
2. **Approve Once** → executes and resumes; **Block Action** → records a human block and continues; **Block Agent** → stops the run (`POST /api/stop`).
3. The pending approval is held in process memory (`RUNS` dict) — the dashboard shows it; it does not survive a restart.

### 3.4 Reviewing events and reports
1. **Forensic Timeline** — filter by All / Allowed / Approval Required / Blocked; search box; click an event for details; Export JSON.
2. **Reports** — executive summary, most-targeted tools, blocked-attack summary; Download JSON / Text.
3. **Compliance & Audit** — audited events, blocked events, approval requests, policy coverage, compliance score; JSON / CSV / timeline export.

---

## 4. Functional requirements

| ID | Requirement | Status |
|---|---|---|
| FR-01 | API receives a proposed tool call and returns a decision before execution | Implemented and tested (end-to-end demo) |
| FR-02 | Risk score 0–100 with bands ALLOW (0–24), REQUIRE_APPROVAL (25–79), BLOCK (80+) | Implemented and tested |
| FR-03 | Policy engine: allowed / denied / approval-required tool lists | Implemented and tested |
| FR-04 | Indirect prompt-injection pattern detection (ignore-previous, reveal-system-prompt, send-to-@, delete-ticket, exfiltrate) | Implemented and tested |
| FR-05 | Sensitive-data detection (emails, customer records, salary, API keys, passwords, CC-like numbers) | Implemented and tested |
| FR-06 | Context heuristics (unrelated actions, suspicious sequences like search_documents → send_external_message) | Implemented and tested |
| FR-07 | Human in the loop: pause at REQUIRE_APPROVAL, approve/block/resume, stop run | Implemented (API + UI); scriptable demo modes do not exercise it |
| FR-08 | Resumable run: pause at approval, resume with the same session | Implemented (API + UI) |
| FR-09 | Append-only forensic log with event id, timestamp, run id, args, verdict, risk, reasons, matched rules, untrusted content, result/block reason | Implemented and tested (hash chain verified via GET /api/verify) |
| FR-10 | Demo modes: normal, attack, ai (provider-backed with scripted fallback) | Implemented and tested |
| FR-11 | Multi-provider AI (nous/openrouter/openai/gemini) with fallback | Implemented and tested (mocked, no live provider comparison) — **organizer confirmation**: Google AI Studio (Gemini API) counts for the Google-environment criterion; additional Google platforms would be even better |
| FR-12 | Reports: executive summary, targeted tools, blocked summary, JSON/Text download | Implemented (UI only; export contents not independently verified by judges) |
| FR-13 | Threat intelligence panel: poisoned email with injection highlights, sender, blocked payload | Implemented and demo-verified |
| FR-14 | Live dashboard: 12 pages, dark/light theme, Ctrl+K search, notifications, stats, gauge, threat feed | Implemented and demo-verified (responsive at 390/768/1024/1440) |

---

## 5. Non-functional requirements

| Category | Target / measured | Notes |
|---|---|---|
| Security | Policy decision is deterministic and local — the agent cannot influence it | Heuristics are regex-based, not semantic |
| Reliability | Demo runs finish deterministically; scripted fallback kicks in on provider failure | Provider calls never raise — structured error returned |
| Usability / accessibility | 12 pages, dark/light theme, focus-visible styles, `prefers-reduced-motion`, responsive 390–1440px | No build step; no framework |
| Performance | Demo run under ~30s at the demo settings (AI timeout 20s, max 256 output tokens, max 1 retry) | Caching: identical requests answered from in-process memory |
| Observed | 66 unit tests pass (`unittest discover -s tests -v`), all mocked | No live provider/network test runs in CI |
| Target | CI suite on pull request; lint on the Python files | No lint config present yet — planned |

---

## 6. Acceptance criteria

Observable and testable:
1. **Safe demo** ends `completed`; all decisions `ALLOW`; dashboard shows no threats. *(verified by UI check)*
2. **Attack demo** ends `blocked`; two blocked actions recorded with risk 100 and correct reasons. *(verified by UI check)*
3. `GET /api/health` returns `status: ok`, provider name, model, `frontend_index_exists: true`. *(verified live)*
4. `GET /api/verify` returns hash-chain verification fields (`ok`, `total`, `verified`). *(verified)*
5. Forensics log appends a JSON line per decision; count matches `GET /api/forensics.count`. *(verified)*
6. `POST /api/stop` with a run ID halts the run (blocked status, stop reason recorded). *(tests pass)*
7. `GET /api/provider/config` returns secret-free provider info; `/api/provider/verify` probes a live provider. *(verified, mocked provider boundary only)*
8. No secrets in API responses or `/api/provider/*` output. *(verified via test `test_describe_provider_is_secret_free`)*

---

## 7. Current limitations and assumptions

- **Pattern matching, not semantics** — paraphrased injections can evade the regex rules.
- **Simulated tools** — the demo tools are stand-ins; real integrations would need credentials and their own audit logs.
- **In-memory sessions** — `RUNS` lives in process memory; restart loses pending approvals. The forensic JSONL survives restarts.
- **No API authentication** — open by design for the local demo; do not expose to a network.
- **Risk scores are heuristic** — weights and thresholds are policy choices.
- **AI mode trusts the model's JSON schema** — malformed output is retried once, then scripted fallback.
- **Preview-only items** — the approval queue, reports exports, and compliance score are demo UI; behavior not fully reviewed by judges.
- **Multi-provider comparison** — Gemini/OpenRouter/OpenAI have never been live-run in this session; only Nous was verified live. Gemini integration is configured but **not live-verified** (see TRD §5.1).
- **Deployment** — Render deployment is documented as a proposal; no cloud resources were created.
- **Google environment criterion** — Organizers confirmed Google AI Studio (the Gemini API) counts for the Google-environment criterion. The project integrates the Gemini API only; it was not built in Google AI Studio. Additional Google platforms (Stitch, Antigravity) would be even better per the organizers and are assessed as future scope only (PRD §8, TRD §5.1).

---

## 8. Future scope (separate from implemented functionality)

- Semantic injection detection (embeddings/classifier).
- Data-loss-prevention engine with per-recipient allow-lists.
- Policy-as-code (signed YAML/JSON with versioning).
- Production transports (IMAP/SMTP, Slack, GitHub, ticket adapters behind the same guard interface).
- Per-agent identities, budgets, and tool scopes.
- Tamper-evident remote log shipping; hash-chained forensics already exist locally.
- WebSocket push for the dashboard instead of polling `GET /api/forensics`.
- Streaming dashboard / explanation mode.
- **Google platform expansion (assessment only; nothing installed)** — Organizers noted that additional Google platforms such as Stitch or Antigravity would be even better for the Google-environment criterion. No install or integration was performed. Any of these would be added only with organizer approval, and only if it makes one small, verifiable contribution to AgentGuard. See TRD §5.1/§16 for the evidence-based assessment.

---

## 9. Hackathon contribution

**Evidence basis:** `git log` (8 commits), untracked working tree, verified timestamped backups, and live API checks performed on 2026-11-11/13.

| Phase | Content | Evidence |
|---|---|---|
| Pre-existing baseline | Initial project (`7936e01`), README, design-system commits `d0450d5` → `c4b0ff8` | Commit history |
| Hackathon docs | `PROJECT_DOCUMENTATION.md`, `FUTURE_ENHANCEMENTS.md`, `JUDGE_READY_GUIDE.md` | Commits `7826fcc`, `7ce4227` |
| Current (this work) | PRD.md, TRD.md, RENDER_DEPLOYMENT.md; README update with links | This document set |
| Organizer clarification (2026-11-13) | Confirmed that Google AI Studio (Gemini API) counts for the Google-environment criterion; additional Google platforms (Stitch, Antigravity) assessed as future options only | Organizer email / Discord clarification |

**Google environment clarification (verbatim from organizer):** "Google AI Studio counts for the Google-environment criterion. Using additional Google platforms such as Stitch or Antigravity would be even better."
| Audit & fix pass (2026-11-11) | `providers.py` (multi-provider), `redteam.py` (attack generator), `tests/` (66 regression tests) — **untracked**, no commit exists | Git status: `?? providers.py`, `?? redteam.py`, `?? tests/` — **timing "not verified"** via git |

**No commit exists for the audit-and-fix files.** If the organizer asks when that work was done, the honest answer is: `git log` shows no commit for `providers.py`, `redteam.py`, or `tests/` — commit timestamps are "not verified"; the files were present in the working tree with a verified backup as of 2026-11-11 14:17 UTC.

---

## Appendix A — Status legend
- ✅ **Implemented and tested** — exists and verified here.
- 🔶 **Implemented but not verified** — exists; live verification not performed (or not by me).
- 🔸 **Partial / simulated** — exists but incomplete or simulated.
- ⬜ **Planned or missing** — roadmap item (see FUTURE_ENHANCEMENTS.md) or not present.
- 🔵 **Configured but not live-verified** — the capability exists (env var, API surface, tests) but no live API call or provider token was used in this session; live verification requires operator approval and a valid key.
