# AgentGuard — Render Deployment Guide

**Status:** deployment plan + unverified steps for the hackathon judging
**Purpose:** get AgentGuard running on Render with the *existing* app — no rewrite, no dependency changes.

> Note: no Render resources were created in this session. All steps below are
> verified as correct for the current code by local health checks, but the
> live Render deployment was not performed and must be treated as **not verified**.

---

## 1. Recommended architecture (single service)

| Concern | Recommendation |
|---|---|
| Backend + frontend | **One Render Web Service** running `uvicorn main:app` |
| Static frontend | Served by FastAPI at `/` and `/{filename}` — no separate static host |
| Database | None — demo uses local `data/` JSON and `logs/forensics.jsonl` |
| Provider keys | Set as environment variables in the Render dashboard (never in code) |
| Persistence | Demos write to `logs/` and `data/`. Render's filesystem is **ephemeral** unless you mount a persistent volume — see Section 4 |

### Why this architecture is the smallest safe change
- `main.py` already dispatches `GET /` → `FileResponse(frontend/index.html)` and
  `GET /{filename}` → static assets. Nothing needs to move.
- The existing `vercel.json` (built on Vercel) is **not** used on Render; Render
  uses the `render.yaml` below.
- The API contract, policy engine, forensic log, and demo modes are untouched.

---

## 2. Step-by-step deployment (untested live)

1. **Repo:** push the project (or connect your repo) to GitHub/GitLab/Bitbucket.
2. **Create a Render Web Service** and connect the repository.
3. **Runtime:** `python` (Render's `python` runtime auto-detects the `requirements.txt`).
4. **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - `--host 0.0.0.0` is required: Render's proxy forwards traffic to any listener
     on the `$PORT` given by Render, and the health probe is against the app itself.
5. **Build command:** leave empty / rely on `pip install -r requirements.txt`.
   - `requirements.txt` lists `fastapi`, `uvicorn`, `pydantic`, `requests`, `python-dotenv`.
6. **Health check path:** `/api/health` (FastAPI endpoint returns `status: ok` with
   provider, model, and frontend file existence flags).
   - Render Web Services need a `/` or `/health` path; `/api/health` is the chosen probe.
7. **Environment variables** (names only — set values in the Render dashboard):

   | Variable | Purpose | Required? |
   |---|---|---|
   | `AI_PROVIDER` | `gemini` / `groq` / `openrouter` / `openai` / `nous` | Yes (default `gemini`) |
   | `AI_MODEL` | Model id | No (default `gemini-3.8-flash` for gemini) |
   | `GEMINI_API_KEY` | Gemini bearer token | If `gemini` |
   | `GROQ_API_KEY` | Groq bearer token | If `groq` |
   | `NOUS_API_KEY` | (legacy) Nous Portal token | If `nous` (or fallback) |
   | `OPENROUTER_API_KEY` | OpenRouter token | If `openrouter` |
   | `OPENAI_API_KEY` | OpenAI token | If `openai` |
   | `GEMINI_API_KEY` | Gemini (Google AI Studio) token | If `gemini` — Google AI Studio counts for the Google-environment criterion per organizer confirmation |
   | `AI_TIMEOUT` | Provider timeout seconds (default 20) | No |
   | `AI_MAX_RETRIES` | Provider retry count (default 1) | No |
   | `AI_MAX_TOKENS` | Output budget per model call (default 256) | No |
   | `AI_CACHE` | In-process cache count (default 1) | No |
   | `PORT` | Render-assigned port | **Do not set** (Render injects it) |

8. **Deploy.** Wait for the health check to pass; the service becomes live.

---

## 3. Post-deploy smoke tests

Run these against the public Render URL:

```bash
# 1) Health — confirm backend + static frontend + provider metadata
curl -s http://<RENDER-URL>/api/health | python3 -m json.tool

# 2) CLI / 404 surface — static assets are served by the same app
curl -s -o /dev/null -w "%{http_code}\n" http://<RENDER-URL>/frontend/styles.css
curl -s -o /dev/null -w "%{http_code}\n" http://<RENDER-URL>/missing.js

# 3) Forensics readback — the log is written by the demo run
curl -s http://<RENDER-URL>/api/forensics | python3 -c "import json,sys; d=json.load(sys.stdin); print('events:', d['count'])"

# 4) Hash-chain verification
curl -s http://<RENDER-URL>/api/verify | python3 -m json.tool
```

> All provider calls remain **mocked** in the test suite (`tests/test_providers.py`),
> so the deployment itself can be verified without paid provider keys.

### What still needs verification on Render (NOT VERIFIED)
- **Ephemeral filesystem:** `logs/forensics.jsonl` and `data/*.json` are written to the
  project directory. On Render these are lost on a deploy/restart unless a **persistent
  disk** is mounted (drag-and-drop volume, or set `LOG_DIR=/tmp`-style env config).
  - Local code uses `VERCEL` env var to pick `/tmp`; on Render that variable is not set,
    so logs go to `logs/` in the project directory.
- **Provider timeouts** under Render's proxy / idle timeouts.
- **`AI_CACHE` / `AI_WARM`** behaviour across a cold deploy cycle.
- Whether the approved demo access to the provider keys works behind Render's egress.

---

## 4. Persistence / storage notes

| Data | Directory | Survives restart? |
|---|---|---|
| Forensic log (system of record) | `logs/forensics.jsonl` | Only if a storage volume is attached |
| Demo data | `data/documents.json`, `data/emails.json` | Only if a storage volume is attached |
| In-memory sessions (`RUNS`) | app memory | No — restart always loses pending approvals |

> If persistent storage is required, attach a Render **disk** and set `LOG_DIR` to the
> mount point. The demo's local default is git-ignored, so no secrets are exposed.

---

## 5. Rollback plan

1. Render Web Services keep every previous deployment revision under **Deployments**.
2. **Roll back** to the last known-good revision from the dashboard (or via API: set
   `CURRENT_REVISION` / use the `render rollback` CLI).
3. Verify `/api/health` and `/api/verify` once the old revision is live.

---

## 6. `render.yaml.example` (draft, untested)

```yaml
services:
  - type: web
    name: agentguard
    runtime: python
    plan: starter
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /api/health
    envVars:
      - key: AI_PROVIDER
        value: gemini
      - key: AI_TIMEOUT
        value: "20"
      - key: AI_MAX_RETRIES
        value: "1"
      - key: AI_MAX_TOKENS
        value: "256"
      - key: AI_CACHE
        value: "1"
    # --- DO NOT hardcode secrets here. ---
    # Add provider keys via the Render dashboard > Environment.
    #
    # Example (do not copy):
    # - key: GEMINI_API_KEY
    #   sync: false
```

---

## 7. Vercel (existing baseline, unchanged)

The committed `vercel.json` (build `@vercel/python`, rewrites to `main.py`) remains
the instruction for the **existing** Vercel deployment. It is not modified by this guide.

---

## 8. Status of this deployment guide

- ✅ `main.py` serves frontend + API from one process — verified locally
- ✅ `GET /api/health` works — verified locally (`status: ok`, frontend present)
- ✅ Test suite 66/66 — verified locally, all provider calls mocked
- ⬜ Live Render deployment — **not performed** in this session; smoke tests above
  must be run against the live service before relying on it for judging.
- **Google environment criterion** — `AI_PROVIDER=gemini` selects the Google AI Studio
  (Gemini API). Organizer confirmation: Google AI Studio counts for the Google-environment
  criterion. The project integrates the Gemini API only; it was not built in Google AI Studio.
