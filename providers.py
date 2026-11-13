"""AgentGuard AI provider configuration and adapter.

Supports four OpenAI Chat-Completions-compatible providers:

    nous | openrouter | openai | gemini

Design rules (see the provider-support spec):
  * ``AI_PROVIDER`` selects the provider (default ``nous``).
  * ``AI_MODEL`` overrides the model id.
  * Only the *selected* provider's key is required.
  * Keys are read from the backend environment only - never from the client.
  * Base URLs are hardcoded in :data:`PROVIDER_SPECS` - never user-supplied.
  * A provider failure never silently switches to another provider; the caller
    falls back to the deterministic scripted agent and labels the run.

The adapter normalises every provider into the agent's existing tool-call
representation (``{"tool": ..., "arguments": ..., "reason": ...}``) and, when
the provider supports native tool calling, preserves ``tool_call_id``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter

# ---------------------------------------------------------------------------
# HTTP transport: one pooled session per process
# ---------------------------------------------------------------------------
#
# Reusing a requests.Session keeps the TLS connection to the provider alive
# across agent turns, so a multi-step run pays the handshake cost once instead
# of once per tool call. The pool is bounded and the adapter's own retries are
# disabled because chat_completion implements its own backoff policy.

_SESSION: requests.Session | None = None
_SESSION_LOCK = threading.Lock()


def _get_session() -> requests.Session:
    """Return the process-wide pooled session, creating it on first use."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                session = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=8,
                    pool_maxsize=8,
                    max_retries=0,
                )
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                _SESSION = session
    return _SESSION


def _post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: int):
    """The single HTTP seam used by :func:`chat_completion`.

    Production traffic goes through the pooled session; tests patch this
    function, so the retry/backoff logic stays exercisable without network.
    """
    return _get_session().post(url, headers=headers, json=json, timeout=timeout)


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------
#
# Repeated demo runs send an identical first request (same system prompt, same
# task, same tool schema). Answering that from memory turns the first model
# call of every repeat run into an instant hit. Only successful results are
# cached - failures must stay visible so the fallback path is exercised.

_CACHE: OrderedDict[str, ProviderResult] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256


def _cache_key(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
) -> str:
    blob = json.dumps(
        {
            "model": model,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> ProviderResult | None:
    with _CACHE_LOCK:
        result = _CACHE.get(key)
        if result is not None:
            _CACHE.move_to_end(key)
    return result


def _cache_put(key: str, result: ProviderResult) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = result
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def clear_cache() -> None:
    """Drop every cached provider response (tests / demo reset)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _copy_result(result: ProviderResult) -> ProviderResult:
    """Deep-enough copy so a cache hit cannot be mutated by a caller."""
    return ProviderResult(
        content=result.content,
        tool_calls=[dict(call) for call in result.tool_calls],
        error_kind=result.error_kind,
        error_message=result.error_message,
        raw=result.raw,
        model=result.model,
        provider=result.provider,
        latency_ms=result.latency_ms,
    )

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderConfigError(RuntimeError):
    """Raised when the selected provider is unknown or misconfigured.

    The message is safe to show to an operator: it never contains a key value.
    """


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

# Blank / obvious placeholder values. A key that looks like one of these is
# treated as "not configured" so the demo can fall back instead of sending a
# bogus credential to a provider.
PLACEHOLDER_MARKERS = (
    "your_",
    "your-",
    "placeholder",
    "xxx",
    "example",
    "<",
    ">",
    "redacted",
    "changeme",
    "change_me",
    "todo",
    "replace_me",
    "replace-me",
    "not_set",
    "notset",
    "dummy",
    "test_key",
)


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of one provider."""

    key_env: str
    base_url: str
    default_model: str
    token_param: str = "max_tokens"
    extra_headers: dict[str, str] = field(default_factory=dict)
    supports_native_tools: bool = True
    notes: str = ""


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "nous": ProviderSpec(
        key_env="NOUS_API_KEY",
        base_url="https://inference-api.nousresearch.com/v1/chat/completions",
        default_model="meituan/longcat-2.5:free",
        token_param="max_tokens",
        notes="Nous Portal - OpenAI-compatible chat completions.",
    ),
    "openrouter": ProviderSpec(
        key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1/chat/completions",
        default_model="openai/gpt-4o-mini",
        token_param="max_tokens",
        # Optional attribution headers documented by OpenRouter. They are not
        # required, so they are left empty unless the operator sets them.
        extra_headers={},
        notes="OpenRouter - OpenAI-compatible chat completions.",
    ),
    "openai": ProviderSpec(
        key_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1/chat/completions",
        default_model="gpt-4o-mini",
        # Current OpenAI models accept max_completion_tokens; older ones accept
        # max_tokens. Kept explicit so capability differences are visible.
        token_param="max_completion_tokens",
        notes="OpenAI Chat Completions.",
    ),
    "gemini": ProviderSpec(
        key_env="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        default_model="gemini-2.0-flash",
        token_param="max_tokens",
        notes="Gemini via its documented OpenAI-compatible interface.",
    ),
}

DEFAULT_PROVIDER = "nous"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved, ready-to-use provider configuration."""

    provider: str
    model: str
    api_key: str
    base_url: str
    extra_headers: dict[str, str] = field(default_factory=dict)
    supports_native_tools: bool = True
    token_param: str = "max_tokens"
    # Lower defaults keep a demo run snappy: a tool-call turn needs only a
    # couple of hundred tokens, and a fast fallback beats a long hang.
    timeout: int = 20
    max_retries: int = 1
    max_tokens: int = 256


@dataclass
class ProviderResult:
    """Normalised provider response.

    Either ``content``/``tool_calls`` are populated on success, or
    ``error_kind``/``error_message`` describe the failure.
    """

    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error_kind: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None
    model: str | None = None
    provider: str | None = None
    latency_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.error_kind is None


def _clean(value: str | None) -> str:
    return (value or "").strip()


def is_placeholder(value: str | None) -> bool:
    """True when a key is blank or an obvious placeholder."""

    cleaned = _clean(value)
    if not cleaned:
        return True
    lowered = cleaned.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _hermes_nous_key() -> str | None:
    """Reuse the Nous Portal OAuth token Hermes already holds, if present.

    The Nous credential is short-lived and refreshed by Hermes, so reading it
    at resolve time keeps AgentGuard current without copying a secret into
    .env. Returns ``None`` when the file is absent or holds no usable token.
    """
    path = os.path.expanduser(os.getenv("HERMES_AUTH_FILE", "~/.hermes/auth.json"))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None

    candidates: list[Any] = []
    nous_block = (data.get("providers") or {}).get("nous")
    if isinstance(nous_block, dict):
        candidates.append(nous_block)
    pool = data.get("credential_pool")
    if isinstance(pool, dict):
        entries = pool.get("nous")
        if isinstance(entries, list):
            candidates.extend(entries)

    for block in candidates:
        if not isinstance(block, dict):
            continue
        for field_name in ("access_token", "agent_key"):
            value = _clean(block.get(field_name))
            if value and not is_placeholder(value):
                return value
    return None


def _env_int(name: str, default: int) -> int:
    raw = _clean(os.getenv(name))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def resolve_provider(provider: str | None = None, model: str | None = None) -> ProviderConfig:
    """Resolve and validate the selected provider.

    Raises :class:`ProviderConfigError` with an actionable, secret-free message
    when the provider is unknown or its key is missing/placeholder.
    """

    selected = _clean(provider) or _clean(os.getenv("AI_PROVIDER")) or DEFAULT_PROVIDER
    selected = selected.lower()

    if selected not in PROVIDER_SPECS:
        known = ", ".join(sorted(PROVIDER_SPECS))
        raise ProviderConfigError(
            f"Unknown AI provider '{selected}'. Known providers: {known}. "
            "Set AI_PROVIDER to one of them."
        )

    spec = PROVIDER_SPECS[selected]
    api_key = _clean(os.getenv(spec.key_env))

    # Nous fallback: Hermes keeps a fresh OAuth token in ~/.hermes/auth.json.
    if is_placeholder(api_key) and selected == "nous":
        api_key = _hermes_nous_key() or ""

    if is_placeholder(api_key):
        hint = (
            " Hermes can also supply this key automatically when you sign in to "
            "the Nous Portal."
            if selected == "nous"
            else ""
        )
        raise ProviderConfigError(
            f"Provider '{selected}' is selected but {spec.key_env} is missing or a "
            f"placeholder. Set {spec.key_env} in .env (or switch AI_PROVIDER to a "
            f"provider whose key is configured).{hint}"
        )

    # Operator-side overrides: AI_MODEL / <PROVIDER>_MODEL and
    # <PROVIDER>_BASE_URL. Never read from the request body.
    env_model = _clean(os.getenv("AI_MODEL")) or _clean(os.getenv(f"{selected.upper()}_MODEL"))
    resolved_model = _clean(model) or env_model or spec.default_model
    if not resolved_model:
        raise ProviderConfigError(
            f"Provider '{selected}' has no model configured. Set AI_MODEL."
        )

    base_url = _clean(os.getenv(f"{selected.upper()}_BASE_URL")) or spec.base_url

    return ProviderConfig(
        provider=selected,
        model=resolved_model,
        api_key=api_key,
        base_url=base_url,
        extra_headers=dict(spec.extra_headers),
        supports_native_tools=spec.supports_native_tools,
        token_param=spec.token_param,
        timeout=_env_int("AI_TIMEOUT", 20),
        max_retries=_env_int("AI_MAX_RETRIES", 1),
        max_tokens=_env_int("AI_MAX_TOKENS", 256),
    )


def describe_provider(provider: str | None = None) -> dict[str, Any]:
    """Secret-free description of the configured provider (for /api/health)."""

    selected = (_clean(provider) or _clean(os.getenv("AI_PROVIDER")) or DEFAULT_PROVIDER).lower()
    if selected not in PROVIDER_SPECS:
        return {
            "provider": selected,
            "known": False,
            "configured": False,
            "model": None,
            "key_env": None,
            "error": f"Unknown provider '{selected}'.",
        }

    spec = PROVIDER_SPECS[selected]
    api_key = _clean(os.getenv(spec.key_env))
    key_source = "env"
    if is_placeholder(api_key) and selected == "nous":
        api_key = _hermes_nous_key() or ""
        key_source = "hermes_auth"
    env_model = _clean(os.getenv("AI_MODEL")) or _clean(os.getenv(f"{selected.upper()}_MODEL"))
    model = env_model or spec.default_model
    return {
        "provider": selected,
        "known": True,
        "configured": not is_placeholder(api_key),
        "model": model,
        "key_env": spec.key_env,
        "key_source": key_source,
        "base_url": _clean(os.getenv(f"{selected.upper()}_BASE_URL")) or spec.base_url,
        "supports_native_tools": spec.supports_native_tools,
        "notes": spec.notes,
    }


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------


def _headers(config: ProviderConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(config.extra_headers)
    return headers


def _payload(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.2,
        config.token_param: max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _parse_completion(data: dict[str, Any], config: ProviderConfig) -> ProviderResult:
    """Normalise an OpenAI-compatible chat-completion body."""

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ProviderResult(
            error_kind="invalid_response",
            error_message="Provider returned a response without choices[0].message.",
            raw=data,
            model=config.model,
            provider=config.provider,
        )

    content = message.get("content")
    tool_calls = message.get("tool_calls") or []

    normalised: list[dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") or {}
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return ProviderResult(
                    error_kind="invalid_arguments",
                    error_message=f"Tool '{name}' arguments were not valid JSON.",
                    raw=data,
                    model=config.model,
                    provider=config.provider,
                )
        normalised.append(
            {
                "id": call.get("id"),
                "type": call.get("type", "function"),
                "name": name,
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )

    return ProviderResult(
        content=content,
        tool_calls=normalised,
        raw=data,
        model=data.get("model", config.model),
        provider=config.provider,
    )


def _retry_delay(attempt: int, response: requests.Response | None) -> float:
    """Backoff for retry ``attempt`` (0-based). Honours Retry-After on 429."""

    if response is not None and response.status_code == 429:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(0.5, min(30.0, float(header)))
            except ValueError:
                pass
    return min(4.0, 0.5 * (2**attempt))


def chat_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    config: ProviderConfig | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
) -> ProviderResult:
    """Call the configured provider and return a normalised result.

    Never raises for API failures - the failure is returned as a structured
    ``error_kind`` so the agent can label the fallback accurately.

    Successful responses are cached in-process (disable with ``AI_CACHE=0``),
    so an identical request - typically the first turn of a repeated demo run
    - is answered from memory instead of the network.
    """

    if config is None:
        config = resolve_provider()

    if max_tokens is None:
        max_tokens = config.max_tokens

    attempt_limit = config.max_retries if max_retries is None else max_retries
    request_timeout = config.timeout if timeout is None else timeout
    url = config.base_url
    headers = _headers(config)
    payload = _payload(config, messages, tools, max_tokens)

    cache_enabled = _clean(os.getenv("AI_CACHE", "1")) not in ("0", "false", "no", "off")
    cache_key = _cache_key(config.model, messages, tools, max_tokens, payload["temperature"])
    if cache_enabled:
        cached = _cache_get(cache_key)
        if cached is not None:
            hit = _copy_result(cached)
            hit.latency_ms = 0
            return hit

    started = time.monotonic()
    last_response: requests.Response | None = None

    for attempt in range(attempt_limit + 1):
        try:
            response = _post(url, headers=headers, json=payload, timeout=request_timeout)
        except requests.Timeout:
            if attempt < attempt_limit:
                time.sleep(_retry_delay(attempt, None))
                continue
            return ProviderResult(
                error_kind="timeout",
                error_message=(
                    f"{config.provider} did not respond within {request_timeout}s."
                ),
                model=config.model,
                provider=config.provider,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except requests.RequestException as exc:
            return ProviderResult(
                error_kind="network",
                error_message=f"Could not reach {config.provider}: {type(exc).__name__}.",
                model=config.model,
                provider=config.provider,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        last_response = response

        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                return ProviderResult(
                    error_kind="invalid_response",
                    error_message="Provider returned a non-JSON body.",
                    model=config.model,
                    provider=config.provider,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            result = _parse_completion(data, config)
            result.latency_ms = int((time.monotonic() - started) * 1000)
            if cache_enabled and result.ok:
                _cache_put(cache_key, result)
            return result

        if response.status_code in (401, 403):
            return ProviderResult(
                error_kind="auth",
                error_message=(
                    f"{config.provider} rejected the credential (HTTP {response.status_code}). "
                    f"Check {PROVIDER_SPECS[config.provider].key_env}."
                ),
                model=config.model,
                provider=config.provider,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        if response.status_code == 429:
            if attempt < attempt_limit:
                time.sleep(_retry_delay(attempt, response))
                continue
            return ProviderResult(
                error_kind="rate_limit",
                error_message=(
                    f"{config.provider} rate limit reached (HTTP 429) after "
                    f"{attempt_limit + 1} attempts."
                ),
                model=config.model,
                provider=config.provider,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        if response.status_code >= 500:
            if attempt < attempt_limit:
                time.sleep(_retry_delay(attempt, response))
                continue
            return ProviderResult(
                error_kind="server",
                error_message=(
                    f"{config.provider} returned HTTP {response.status_code} after "
                    f"{attempt_limit + 1} attempts."
                ),
                model=config.model,
                provider=config.provider,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        # 4xx other than auth/rate-limit: not retryable.
        detail = ""
        try:
            detail = str(response.json().get("error", {}).get("message", ""))[:200]
        except Exception:
            detail = response.text[:200]
        return ProviderResult(
            error_kind="bad_request",
            error_message=(
                f"{config.provider} returned HTTP {response.status_code}"
                + (f": {detail}" if detail else ".")
            ),
            model=config.model,
            provider=config.provider,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # Defensive: the loop always returns, but keep the type-checker happy.
    return ProviderResult(
        error_kind="network",
        error_message=f"Exhausted retries against {config.provider}.",
        model=config.model,
        provider=config.provider,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def verify_provider(
    provider: str | None = None,
    model: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Probe the configured provider with a minimal request.

    Returns a secret-free status dict. Never raises.
    """

    try:
        config = resolve_provider(provider, model)
    except ProviderConfigError as exc:
        return {
            "verified": False,
            "provider": (provider or os.getenv("AI_PROVIDER") or DEFAULT_PROVIDER).lower(),
            "error_kind": "config",
            "error": str(exc),
        }

    result = chat_completion(
        [{"role": "user", "content": "ping"}],
        config=config,
        max_tokens=8,
        timeout=timeout,
        max_retries=0,
    )

    if not result.ok:
        return {
            "verified": False,
            "provider": config.provider,
            "model": config.model,
            "error_kind": result.error_kind,
            "error": result.error_message,
            "latency_ms": result.latency_ms,
        }

    return {
        "verified": True,
        "provider": config.provider,
        "model": config.model,
        "latency_ms": result.latency_ms,
    }
