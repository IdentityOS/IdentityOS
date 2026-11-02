"""Multi-provider coder client for ratchet autopilot proposals.

Order (override with AUTOPILOT_CODER_ORDER):
  gemini → groq → deepseek

Env (prefer benchmarks/.env):
  GEMINI_API_KEY / GOOGLE_API_KEY + optional GEMINI_MODEL (default gemini-3.6-flash)
  GROQ_API_KEY (+ GROQ_API_KEY_2…) + optional GROQ_CODER_MODEL (default openai/gpt-oss-120b)
  DEEPSEEK_API_KEY + optional DEEPSEEK_CODER_MODEL (default deepseek-v4-flash)
  OLLAMA_CODER_MODEL + optional OLLAMA_CODER_BASE_URL (local, opt-in)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import httpx

from adapters.base import collect_api_keys

DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
# Non-deprecated Groq defaults (llama-3.3-70b-versatile is shut down for free/dev).
# Prefer 20b first — free-tier TPM is tight for large autopilot prompts.
DEFAULT_GROQ_MODEL = os.environ.get("GROQ_CODER_MODEL", "openai/gpt-oss-20b")
GROQ_MODEL_FALLBACKS = (
    DEFAULT_GROQ_MODEL,
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
)
DEFAULT_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_CODER_MODEL", "deepseek-v4-flash")
DEFAULT_ORDER = ("gemini", "groq", "deepseek")


class CoderError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise CoderError(f"Coder response was not JSON: {text[:400]}") from exc
        return json.loads(match.group(0))


def _validate_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    for key in ("hypothesis", "change", "edits"):
        if key not in proposal:
            raise CoderError(f"Coder proposal missing {key!r}")
    if not isinstance(proposal["edits"], list):
        raise CoderError("edits must be a list")
    return proposal


def _coder_order() -> list[str]:
    raw = os.environ.get("AUTOPILOT_CODER_ORDER", "")
    if raw.strip():
        return [p.strip().lower() for p in raw.split(",") if p.strip()]
    return list(DEFAULT_ORDER)


def _call_gemini(prompt: str, *, model: str | None, timeout_s: float) -> dict[str, Any]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise CoderError("GEMINI_API_KEY not set")
    model = model or DEFAULT_GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(url, params={"key": key}, json=payload)
    if resp.status_code >= 400:
        raise CoderError(f"Gemini API error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise CoderError(f"Unexpected Gemini response shape: {data!r}") from exc
    return _validate_proposal(_extract_json(text))


def _openai_compatible_chat(
    *,
    prompt: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_s: float,
    provider_label: str,
    max_tokens: int = 2048,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0.15,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a code-editing agent. Reply with a single JSON object only "
                    "(hypothesis, change, edits, tests_to_run). No markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    if extra_body:
        payload.update(extra_body)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise CoderError(f"{provider_label} API error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise CoderError(f"Unexpected {provider_label} response shape: {data!r}") from exc
    return _validate_proposal(_extract_json(text))


def _call_groq(prompt: str, *, model: str | None, timeout_s: float) -> dict[str, Any]:
    keys = collect_api_keys("GROQ_API_KEY")
    if not keys:
        raise CoderError("GROQ_API_KEY not set")
    models: list[str] = []
    if model:
        models.append(model)
    for m in GROQ_MODEL_FALLBACKS:
        if m not in models:
            models.append(m)
    errors: list[str] = []
    for model_id in models:
        for key in keys:
            try:
                return _openai_compatible_chat(
                    prompt=prompt,
                    base_url="https://api.groq.com/openai/v1",
                    api_key=key,
                    model=model_id,
                    timeout_s=timeout_s,
                    provider_label=f"Groq({model_id})",
                )
            except CoderError as exc:
                errors.append(str(exc))
                # Rotate key / try next model on rate or size errors.
                continue
    raise CoderError("Groq failed all keys/models: " + " | ".join(errors[:4]))


def _call_deepseek(prompt: str, *, model: str | None, timeout_s: float) -> dict[str, Any]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise CoderError("DEEPSEEK_API_KEY not set")
    model = model or DEFAULT_DEEPSEEK_MODEL
    return _openai_compatible_chat(
        prompt=prompt,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        api_key=key,
        model=model,
        timeout_s=timeout_s,
        provider_label="DeepSeek",
        # V4 defaults to thinking mode — disable for cheap JSON edit proposals.
        extra_body={"thinking": {"type": "disabled"}},
    )


def _call_ollama(prompt: str, *, model: str | None, timeout_s: float) -> dict[str, Any]:
    """Request a local code proposal; the frozen ratchet remains the judge."""
    selected = model or os.environ.get("OLLAMA_CODER_MODEL")
    if not selected:
        raise CoderError("OLLAMA_CODER_MODEL not set")
    base = os.environ.get("OLLAMA_CODER_BASE_URL", "http://127.0.0.1:11434/v1")
    if not re.fullmatch(r"http://(?:localhost|127\.0\.0\.1)(?::\d+)?/v1/?", base):
        raise CoderError("Ollama coder endpoint must be local loopback /v1")
    return _openai_compatible_chat(
        prompt=prompt,
        base_url=base,
        api_key="ollama",
        model=selected,
        timeout_s=timeout_s,
        provider_label=f"Ollama({selected})",
        max_tokens=2048,
    )


_PROVIDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "gemini": _call_gemini,
    "groq": _call_groq,
    "deepseek": _call_deepseek,
    "ollama": _call_ollama,
}


def propose_edits(
    prompt: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Try coder providers in order until one returns a valid JSON proposal."""
    if provider:
        order = [provider.lower()]
    else:
        order = _coder_order()

    errors: list[str] = []
    for name in order:
        fn = _PROVIDERS.get(name)
        if fn is None:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            proposal = fn(prompt, model=model if provider else None, timeout_s=timeout_s)
            proposal["_coder_provider"] = name
            return proposal
        except CoderError as exc:
            errors.append(f"{name}: {exc}")
            continue

    raise CoderError(
        "All coder providers failed:\n  - " + "\n  - ".join(errors)
    )
