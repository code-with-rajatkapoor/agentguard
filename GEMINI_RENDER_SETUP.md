# GEMINI_RENDER_SETUP.md — AgentGuard on Render (Gemini)

Make the Gemini (Google AI Studio) configuration deployment-ready for the current project.
All evidence below comes from the actual code in `agentguard/`, from current official
Google documentation, and from mocked tests only — no live API calls were made.

---

## A. Create my key

**Official page:** https://aistudio.google.com/apikey

1. Open https://aistudio.google.com/apikey in **your own** Google account
   (the account you use for the Google-criterion judging).
2. Click **Create API key**. The key lands in your clipboard.
3. Choose the Google AI Studio project/access you want the key counted for
   (if the project asks you to create/select a project, let it create one —
   the key is tied to that account, not to a specific project).
4. **Do not paste the key into chat.** It is a secret. Only paste it into
   Render's private environment variable (see section B).

## B. Add Render environment variables

Service: `agentguard` — Dashboard → Services → **agentguard** → Environment
(or "Environment Variables" tab) → **Add Environment Variable**.

| Variable name | Exact non-secret value / instruction | Required? |
|---|---|---|
| `AI_PROVIDER` | `gemini` | **Yes** — selects the Google AI Studio provider |
| `AI_MODEL` | `gemini-3.8-flash` | **Yes** — the adapter's default model (free tier, per Google AI Studio pricing) |
| `GEMINI_API_KEY` | **Paste your own key privately in Render** | **Yes** — key from section A |
| `AI_TIMEOUT` | `20` | No (optional) |
| `AI_MAX_RETRIES` | `1` | No (optional) |
| `AI_MAX_TOKENS` | `256` | No (optional) |
| `AI_CACHE` | `1` | No (optional) |

**Do not add** `NOUS_MODEL` / `NOUS_API_KEY` / `OPENROUTER_API_KEY` /
`OPENAI_API_KEY` — those belong to other providers and are not required for
Gemini. `GEMINI_API_KEY` is the only API key the Gemini service needs.

## C. Apply configuration

1. **Save** the variables in the Render dashboard (they are stored by Render,
   not in this repo).
2. **Redeploy / restart** the service: Render auto-redeploys on env-var changes
   in most cases; if not, click **Manual Deploy** or trigger a commit to
   `main`. Restart is required for the new values to take effect.
3. **Successful startup** looks like:
   - `/api/health` returns `200` with provider `gemini` and
     model `gemini-2.0-flash`.
   - Render logs show the Gemini adapter starting (endpoint
     `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`).
4. **If configuration is missing**, the app refuses to start with a clear log:
   `AI_PROVIDER is not set`, `GEMINI_API_KEY is missing`, or
   `Key for provider 'gemini' is missing`. Check the Render environment tab
   and the deploy logs, then redeploy.

## D. Test

**Safe deterministic test first (no key needed):**
1. Deploy with `AI_PROVIDER=gemini` and `GEMINI_API_KEY` **left empty**.
2. Call `/api/health` → expect `200` for the server.
3. Ask the agent a question → the run is answered by the **scripted fallback**
   and is labeled `SCRIPTED_FALLBACK` with the reason
   `provider gemini, key missing`. This is the honest "no key" path and is
   what you want before spending a real request.

**Optional live Gemini test (after you approve):**
1. Paste `GEMINI_API_KEY` in Render and redeploy.
2. Run the same question. Real Gemini output appears only when the key is
   valid and the quota allows it.

**How to tell real Gemini from fallback:**
- Log label `SCRIPTED_FALLBACK` → scripted path, no Gemini call.
- Real Gemini → the run is labeled as the selected provider with tool-call
  tracing visible; a failed key/auth shows as an auth error in logs.

## E. What is verified, what is not

- Code/config: verified (actual `agentguard/` files).
- Google documentation: used (AI Studio API key page, Gemini 2.0 flash
  OpenAI-compatible endpoint).
- Mocked/local tests: `tests/test_providers.py` covers provider selection and
  provider error scenarios with responses mocked (no network).
- **Live Gemini testing: NOT VERIFIED** — the sandbox has no Gemini key, and
  no live API calls were made. Deploy first, then run the live test under
  section D after approval.
