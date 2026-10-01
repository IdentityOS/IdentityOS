from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import time as _time
from typing import Any, Optional

from .base import BaseAdapter

logger = logging.getLogger(__name__)

# Matches legacy Anthropic-style function-call syntax that some models
# (e.g. Llama on Groq) emit instead of native JSON tool_calls:
#   <function=executive__start_task>{'goal': '...'}</function>
_LEGACY_FUNCTION_RE = re.compile(r"<function=([^>]+)>(.*?)</function>", re.DOTALL)


def _is_token_limit_error(msg: str) -> bool:
    """True when a provider rejected the request for exceeding a token budget.

    Matches 413 status codes as well as the wording providers use when a
    request exceeds the model's context window or a per-request token-per-minute
    allowance (e.g. Groq free tier).
    """
    lowered = msg.lower()
    return any(tok in lowered for tok in (
        "413",
        "request too large",
        "tokens per minute",
        "context length",
        "context window",
        "maximum context",
        "token limit",
        "reduce your message size",
    ))


def _parse_legacy_function_call(text: str) -> Optional[tuple[str, dict]]:
    """Extract a legacy ``<function=name>{args}</function>`` call from text.

    Returns ``(name, args_dict)`` or ``None``.  Handles both JSON and
    Python-dict-style argument payloads (Groq reports the latter in
    ``failed_generation``).
    """
    m = _LEGACY_FUNCTION_RE.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    raw_args = m.group(2).strip()
    try:
        args = json.loads(raw_args)
    except (json.JSONDecodeError, TypeError):
        try:
            args = ast.literal_eval(raw_args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return name, args


def _parse_failed_generation_tool_call(text: str) -> Optional[tuple[str, dict]]:
    """Extract a structured tool call embedded in a provider error message.

    OpenAI-compatible providers commonly include ``failed_generation`` as a
    quoted Python-repr value.  Its JSON is therefore escaped (for example,
    newlines arrive as the two characters ``\\n``), so attempting to decode the
    surrounding exception text directly is not sufficient.
    """
    candidates: list[str] = []
    marker = re.search(r"[\"']?failed_generation[\"']?\s*:\s*", text)
    if marker:
        value_start = marker.end()
        if value_start < len(text) and text[value_start] in {"'", '"'}:
            quote = text[value_start]
            escaped = False
            for index in range(value_start + 1, len(text)):
                char = text[index]
                if char == quote and not escaped:
                    try:
                        decoded = ast.literal_eval(text[value_start:index + 1])
                    except (SyntaxError, ValueError):
                        decoded = None
                    if isinstance(decoded, str):
                        candidates.append(decoded)
                    break
                if char == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False

    candidates.append(text)
    for candidate in candidates:
        match = re.search(r'\{\s*"name"\s*:', candidate)
        if match is None:
            continue
        try:
            payload, _ = json.JSONDecoder().raw_decode(candidate[match.start():])
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            name = payload.get("name")
            args = payload.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    try:
                        args = ast.literal_eval(args)
                    except (SyntaxError, ValueError):
                        args = {}
            if isinstance(name, str) and isinstance(args, dict):
                return name, args
    return None


def _is_output_parse_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "output_parse_failed" in lowered
        or "failed to parse tool call arguments" in lowered
        or ("parsing failed" in lowered and "failed_generation" in lowered)
    )


def _is_disabled_tool_call_error(text: str) -> bool:
    """Recognize provider rejection even when ``failed_generation`` is corrupt.

    Groq can report that a model called a tool while tool choice was ``none``
    and attach an incomplete JSON fragment.  Parsing that fragment is useful
    when possible, but the provider's structured error message is sufficient
    to establish that no callable tool was offered and no call was executed.
    """
    lowered = text.lower().replace("tool_choice", "tool choice")
    return (
        "tool choice is none" in lowered
        and ("called a tool" in lowered or "tool call" in lowered)
    )


def _runtime_evidence_fallback(evidence: list[tuple[str, str]]) -> str:
    lines = [
        "Model synthesis was unavailable. Verified runtime evidence:",
    ]
    lines.extend(f"- {name}: {result}" for name, result in evidence)
    return "\n".join(lines)


def _no_runtime_evidence_fallback() -> str:
    """Return a truthful terminal response when tool-free synthesis fails."""
    return (
        "The model could not produce a valid plain-text response. "
        "No tool was executed and no external claim was verified."
    )


def _legacy_tool_context(
    context: str,
    user_input: str,
    tools: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> str:
    """Add a compact, relevant text protocol for models without native tools."""
    selected = _select_relevant_tools(user_input, tools, limit=limit)
    if not selected:
        return context

    lines = [
        "## Executable tools",
        "The following tools are real runtime functions. When the request requires one, "
        "call it instead of guessing.",
        "Emit exactly `<function=TOOL_NAME>{JSON_ARGUMENTS}</function>` using the exact "
        "safe tool name below. Do not use a dotted capability name.",
        "Copy the call template for the selected tool and replace only placeholder values. "
        "Never copy schema keys such as properties, required, type, or additionalProperties "
        "into JSON_ARGUMENTS.",
    ]
    for tool in selected:
        function = tool.get("function", {})
        name = function.get("name", "")
        description = function.get("description", "")
        schema = json.dumps(
            function.get("parameters", {"type": "object"}),
            separators=(",", ":"),
            sort_keys=True,
        )
        template = _legacy_call_template(name, function.get("parameters", {}))
        lines.append(
            f"- {name}: {description}\n  call template: {template}\n  argument schema: {schema}"
        )
    return context + "\n\n" + "\n".join(lines)


def _select_relevant_tools(
    user_input: str,
    tools: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Rank tool definitions by lexical relevance without capability-specific rules."""
    input_words = set(re.findall(r"[a-z0-9]+", user_input.lower()))
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for position, tool in enumerate(tools):
        function = tool.get("function", {})
        searchable = " ".join(
            [
                str(function.get("name", "")).replace("__", " ").replace("_", " "),
                str(function.get("description", "")),
            ]
        ).lower()
        tool_words = set(re.findall(r"[a-z0-9]+", searchable))
        score = len(input_words & tool_words)
        exact_name = str(function.get("name", "")).lower()
        dotted_name = exact_name.replace("__", ".")
        if exact_name and (
            exact_name in user_input.lower() or dotted_name in user_input.lower()
        ):
            score += 10
        scored.append((score, -position, tool))

    relevant = [item for item in scored if item[0] > 0]
    if not relevant:
        relevant = scored
    relevant.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in relevant[:limit]]


def _legacy_call_template(name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    arguments: dict[str, Any] = {}
    placeholders = {
        "string": "VALUE",
        "integer": 1,
        "number": 1,
        "boolean": True,
        "array": [],
        "object": {},
    }
    for parameter in required:
        prop = properties.get(parameter, {})
        expected = prop.get("type", "string")
        if isinstance(expected, list):
            expected = next((item for item in expected if item != "null"), "string")
        arguments[parameter] = placeholders.get(expected, "VALUE")
    return f"<function={name}>{json.dumps(arguments, separators=(',', ':'))}</function>"


def _parse_json_object(raw: str) -> Optional[dict[str, Any]]:
    """Parse a JSON or Python-literal object; return None on failure."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            value = ast.literal_eval(raw)
        except Exception:
            return None
    return value if isinstance(value, dict) else None


def _tool_name_aliases(name: str) -> list[str]:
    aliases = [name]
    if "__" in name:
        aliases.append(name.replace("__", "."))
    elif "." in name:
        aliases.append(name.replace(".", "__"))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _balanced_object(text: str, start: int) -> Optional[tuple[str, int]]:
    """Return a balanced object literal and its exclusive end offset."""
    position = start
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != "{":
        return None
    depth = 0
    quote = ""
    escaped = False
    for index in range(position, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[position:index + 1], index + 1
    return None


def _known_text_tool_calls(
    text: str,
    tools: list[dict[str, Any]],
) -> list[tuple[int, str, dict[str, Any]]]:
    offered = [
        str(tool.get("function", {}).get("name", ""))
        for tool in tools
        if tool.get("function", {}).get("name")
    ]
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for match in _LEGACY_FUNCTION_RE.finditer(text or ""):
        raw_name = match.group(1).strip()
        name = next((item for item in offered if raw_name in _tool_name_aliases(item)), "")
        arguments = _parse_json_object(match.group(2))
        if name and arguments is not None:
            candidates.append((match.start(), name, arguments))

    for name in offered:
        for alias in _tool_name_aliases(name):
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(alias)}\s*\(",
                re.IGNORECASE,
            )
            for match in pattern.finditer(text or ""):
                balanced = _balanced_object(text, match.end())
                if balanced is None:
                    continue
                raw, _ = balanced
                arguments = _parse_json_object(raw)
                if arguments is not None:
                    candidates.append((match.start(), name, arguments))

    candidates.sort(key=lambda item: item[0])
    return candidates[:8]


def _parse_known_text_tool_call(
    text: str,
    tools: list[dict[str, Any]],
) -> Optional[tuple[str, dict[str, Any]]]:
    """Parse the first text-form call for an explicitly offered tool."""
    calls = _known_text_tool_calls(text, tools)
    return (calls[0][1], calls[0][2]) if calls else None


def _parse_all_known_text_tool_calls(
    text: str,
    tools: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """Extract bounded text-form tool calls in model-emitted order."""
    return [(name, arguments) for _, name, arguments in _known_text_tool_calls(text, tools)]

def _resolve_openai_timeout(value: Any) -> float:
    """Return a finite, positive request timeout in seconds."""
    if value is None or value == "":
        return 120.0
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("OPENAI_TIMEOUT must be a positive number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("OPENAI_TIMEOUT must be a positive number of seconds")
    return timeout


class OpenAIAdapter(BaseAdapter):
    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        organization: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        max_tool_rounds: int = 4,
        timeout: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        reasoning_exclude: bool = True,
        extra_body: Optional[dict] = None,
        **kwargs
    ):
        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY")
        super().__init__(model=model, **kwargs)
        self.api_key = api_key
        if base_url is None:
            base_url = os.environ.get("OPENAI_BASE_URL")
        self.base_url = base_url
        self.organization = organization
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.reasoning_effort = reasoning_effort
        self.reasoning_exclude = reasoning_exclude
        self.extra_body = extra_body or {}

        if timeout is None:
            timeout = os.environ.get("OPENAI_TIMEOUT")
        self.timeout = _resolve_openai_timeout(timeout)
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI, Timeout
                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    organization=self.organization,
                    # Use the SDK's public type: newer SDKs can use a different
                    # HTTP transport than the project's direct httpx dependency.
                    timeout=Timeout(timeout=self.timeout, connect=5.0),
                    # Retry policy lives in ``generate`` and provider-specific
                    # adapters. SDK retries would multiply the configured
                    # timeout and make fallback latency unpredictable.
                    max_retries=0,
                )
            except ImportError:
                raise ImportError("openai package not found. Install with: pip install openai")
        return self._client

    def generate(
        self,
        context: str,
        user_input: str,
        identity: Any,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: int = 3,
        **kwargs
    ) -> str:
        # Extract execute_tool from kwargs so it doesn't crash the OpenAI client API
        execute_tool = kwargs.pop("execute_tool", None)
        
        client = self._get_client()
        messages = [
            {"role": "system", "content": context},
            {"role": "user", "content": user_input},
        ]
        model = self.model or "gpt-4o"
        last_exc = None
        effective_max = max_tokens or self.max_tokens
        tool_rounds = 0
        tool_evidence: list[tuple[str, str]] = []
        final_instruction_added = False
        plain_text_recovery_used = False

        # Each tool round requires another full provider request.  Keep that
        # resource use finite and reserve one final request for synthesizing
        # the observed evidence into an answer.
        for _ in range(self.max_tool_rounds + 1):
            if (
                tool_rounds >= self.max_tool_rounds
                and tool_evidence
                and not final_instruction_added
            ):
                messages.append({
                    "role": "user",
                    "content": (
                        "The tool-call budget is exhausted. Do not call another tool. "
                        "Answer now using only the verified tool results above."
                    ),
                })
                final_instruction_added = True
            response = None
            recovered = False
            attempt = 0
            shrinks = 0
            while attempt < retries + shrinks:
                attempt += 1
                try:
                    request_kwargs = dict(kwargs)
                    if tool_rounds >= self.max_tool_rounds or plain_text_recovery_used:
                        request_kwargs.pop("tools", None)
                        request_kwargs.pop("tool_choice", None)
                    tools_enabled_for_request = bool(request_kwargs.get("tools")) and (
                        request_kwargs.get("tool_choice") != "none"
                    )
                    # Add reasoning_effort if configured (for models that support it, e.g., o1, Nemotron)
                    if self.reasoning_effort and "reasoning_effort" not in request_kwargs:
                        request_kwargs["reasoning_effort"] = self.reasoning_effort
                    # Add extra_body if configured (e.g., for NVIDIA Nemotron to disable thinking)
                    if self.extra_body and "extra_body" not in request_kwargs:
                        request_kwargs["extra_body"] = self.extra_body
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature or self.temperature,
                        max_tokens=effective_max,
                        **request_kwargs
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc)
                    msg_lower = msg.lower()
                    if "rate limit" in msg_lower or "429" in msg:
                        if attempt < retries:
                            wait = 2 ** attempt * 5
                            logger.warning("Rate limited, retrying in %ds...", wait)
                            _time.sleep(wait)
                        continue
                    # Providers reject oversized requests (413 / token-budget
                    # exceeded). Shrink the completion budget and retry instead
                    # of hard-failing.
                    if _is_token_limit_error(msg) and effective_max and effective_max > 256:
                        effective_max = max(256, effective_max // 2)
                        shrinks += 1
                        logger.warning(
                            "Token-limit rejection (max_tokens shrunk to %d): %.120s",
                            effective_max, msg,
                        )
                        continue
                    # Some providers reject model-emitted tool syntax before
                    # returning a response. Feed the rejected call through the
                    # runtime gateway so authorized calls execute and unknown
                    # calls become explicit error evidence.
                    rejected_call = (
                        _parse_legacy_function_call(msg)
                        or _parse_failed_generation_tool_call(msg)
                    )
                    model_tool_rejection = (
                        rejected_call is not None
                        or _is_output_parse_error(msg)
                        or _is_disabled_tool_call_error(msg)
                    )
                    if (
                        tool_rounds >= self.max_tool_rounds
                        and tool_evidence
                        and model_tool_rejection
                    ):
                        logger.warning(
                            "Model synthesis failed after verified tool execution; "
                            "returning explicit runtime evidence."
                        )
                        return _runtime_evidence_fallback(tool_evidence)
                    if (
                        not tools_enabled_for_request
                        and model_tool_rejection
                        and not tool_evidence
                        and (
                            rejected_call is None
                            or not execute_tool
                            or tool_rounds >= self.max_tool_rounds
                        )
                    ):
                        if plain_text_recovery_used:
                            logger.warning(
                                "Provider repeated an invalid tool response in "
                                "schema-free mode; returning explicit non-execution evidence."
                            )
                            return _no_runtime_evidence_fallback()
                        # Some OpenAI-compatible providers can reject a model's
                        # attempted tool call when tools were never offered or
                        # were removed after the runtime budget was exhausted.
                        # Retry once with a minimal schema-free context. No call
                        # is executed and no runtime fact is fabricated.
                        messages = [
                            {
                                "role": "system",
                                "content": (
                                    "Reply in plain text without calling tools. "
                                    "No runtime evidence is available. Do not claim "
                                    "an action occurred; state uncertainty when the "
                                    "request requires external evidence."
                                ),
                            },
                            {"role": "user", "content": user_input},
                        ]
                        plain_text_recovery_used = True
                        shrinks += 1
                        logger.warning(
                            "Provider emitted a tool call with tools disabled; "
                            "retrying once in schema-free plain-text mode."
                        )
                        continue
                    recovered_tool = None
                    if execute_tool and tool_rounds < self.max_tool_rounds:
                        recovered_tool = self._recover_rejected_tool_call(
                            msg, messages, execute_tool
                        )
                    if recovered_tool is not None:
                        tool_evidence.append(recovered_tool)
                        tool_rounds += 1
                        recovered = True
                        break
                    if (
                        execute_tool
                        and tool_rounds < self.max_tool_rounds
                        and _is_output_parse_error(msg)
                    ):
                        messages.append({
                            "role": "user",
                            "content": (
                                "The provider could not parse the previous response. "
                                "Do not call a tool. Answer the user directly; when no "
                                "runtime evidence is available, state that you cannot verify the claim."
                            ),
                        })
                        tool_rounds = self.max_tool_rounds
                        recovered = True
                        break
                    raise RuntimeError(
                        f"Adapter error (model={model!r}, base_url={self.base_url!r}): {msg}"
                    ) from exc

            if recovered:
                continue  # loop again with the tool result appended

            if response is None:
                msg = str(last_exc) if last_exc else "Unknown error"
                raise RuntimeError(
                    f"Adapter error (model={model!r}, base_url={self.base_url!r}): {msg}"
                ) from last_exc

            choice = response.choices[0]
            message = choice.message

            # Handle reasoning content (e.g., Nemotron, o1 models)
            reasoning_content = getattr(message, "reasoning_content", None)
            if self.reasoning_exclude and reasoning_content:
                # Reasoning is internal — do not expose to user
                pass

            # --- Tool Calling Loop ---
            if getattr(message, "tool_calls", None) and execute_tool:
                if tool_rounds >= self.max_tool_rounds:
                    raise RuntimeError(
                        f"Adapter tool-call limit reached after {tool_rounds} round(s); "
                        "refusing to execute an unbounded model tool loop."
                    )
                assistant_msg = {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            }
                        } for tc in message.tool_calls
                    ]
                }
                messages.append(assistant_msg)

                for tool_call in message.tool_calls:
                    func_name = tool_call.function.name
                    try:
                        func_args = json.loads(tool_call.function.arguments)
                    except (json.JSONDecodeError, TypeError):
                        func_args = {}

                    tool_result = execute_tool(func_name, func_args)
                    tool_evidence.append((func_name, str(tool_result)))

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(tool_result),
                    })
                tool_rounds += 1
                continue

            content = message.content or ""
            return content

        raise RuntimeError(
            f"Adapter tool-call limit reached after {tool_rounds} round(s) without "
            "a final model response."
        )

    def _recover_rejected_tool_call(
        self,
        error_msg: str,
        messages: list,
        execute_tool: Any,
    ) -> Optional[tuple[str, str]]:
        """Recover from a provider-rejected tool call through the gateway.

        Some models (e.g. Llama on Groq) emit legacy Anthropic-style
        ``<function=name>{args}</function>`` text instead of native JSON
        ``tool_calls``; others attempt an unavailable structured tool.  The
        runtime callback remains authoritative: it executes only an offered,
        authorized tool and returns error evidence for any unknown name.

        Returns the tool name and observed result, or ``None`` when no rejected
        call could be extracted.
        """
        rejected = (
            _parse_legacy_function_call(error_msg)
            or _parse_failed_generation_tool_call(error_msg)
        )
        if rejected is None:
            return None
        func_name, func_args = rejected
        try:
            tool_result = execute_tool(func_name, func_args)
        except Exception as exc:
            tool_result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        messages.append({
            "role": "user",
            "content": f"[tool result for {func_name}]: {tool_result}",
        })
        logger.warning(
            "Recovered provider-rejected tool call %s through the runtime "
            "gateway; retrying model with its observed result.",
            func_name,
        )
        return func_name, str(tool_result)

    def health_check(self) -> bool:
        try:
            client = self._get_client()
            # Try models.list() first (standard OpenAI)
            try:
                client.models.list()
                return True
            except Exception as e:
                # Some providers (Gemini, NVIDIA, etc.) don't support /models endpoint
                # Fall back to a minimal completion request
                error_msg = str(e).lower()
                if any(code in error_msg for code in ("404", "not found", "not supported", "unsupported")):
                    # Try a minimal request instead
                    client.chat.completions.create(
                        model=self.model or "gpt-4o",
                        messages=[{"role": "user", "content": "hi"}],
                        max_tokens=1,
                    )
                    return True
                # Re-raise if it's a different error
                raise
        except Exception:
            return False


class AnthropicAdapter(BaseAdapter):
    def __init__(
        self,
        model: str = "claude-3-5-sonnet-20241022",
        api_key: Optional[str] = None,
        max_tokens: int = 1024,
        **kwargs
    ):
        if api_key is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        super().__init__(model=model, **kwargs)
        self.api_key = api_key
        self.max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                raise ImportError("anthropic package not found. Install with: pip install anthropic")
        return self._client

    def generate(self, context: str, user_input: str, identity: Any, **kwargs) -> str:
        kwargs.pop("execute_tool", None)
        kwargs.pop("tools", None)
        kwargs.pop("tool_choice", None)
        client = self._get_client()
        model = self.model or "claude-3-5-sonnet-20241022"
        try:
            response = client.messages.create(
                model=model, max_tokens=self.max_tokens, system=context,
                messages=[{"role": "user", "content": user_input}], **kwargs
            )
            return response.content[0].text if response.content else ""
        except Exception as exc:
            raise RuntimeError(f"Adapter error (model={model!r}): {exc}") from exc


def _ollama_host_from_base_url(base_url: str) -> str:
    """Derive ``http://host:port`` from an OpenAI-compatible Ollama base URL."""
    trimmed = (base_url or "http://localhost:11434/v1").rstrip("/")
    if trimmed.endswith("/v1"):
        return trimmed[:-3]
    return trimmed


def list_ollama_models(
    base_url: str = "http://localhost:11434/v1",
    timeout: float = 2.0,
) -> list[str]:
    """Return model names reported by a local Ollama server (``ollama list``)."""
    import urllib.error
    import urllib.request

    host = _ollama_host_from_base_url(base_url)
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []

    names = [entry.get("name", "") for entry in payload.get("models", [])]
    return [name for name in names if name]


def ollama_model_capabilities(
    model: str,
    base_url: str = "http://localhost:11434/v1",
    timeout: float = 2.0,
) -> set[str]:
    """Return capabilities advertised for an installed Ollama model."""
    import urllib.error
    import urllib.request

    host = _ollama_host_from_base_url(base_url)
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return set()

    models = payload.get("models", [])
    resolved = resolve_ollama_model(
        model,
        [entry.get("name", "") for entry in models if entry.get("name")],
    )
    entry = next((item for item in models if item.get("name") == resolved), None)
    if entry is None:
        return set()
    return {
        str(capability)
        for capability in entry.get("capabilities", [])
        if capability
    }


def resolve_ollama_model(preferred: str, models: list[str]) -> Optional[str]:
    """Map *preferred* to an installed Ollama model name, if possible."""
    if not models:
        return preferred or None
    if not preferred:
        return models[0]

    if preferred in models:
        return preferred

    pref_base = preferred.split(":", 1)[0]
    exact_tag = [m for m in models if m.split(":", 1)[0] == pref_base]
    if len(exact_tag) == 1:
        return exact_tag[0]
    if len(exact_tag) > 1:
        tagged = [m for m in exact_tag if m.startswith(preferred)]
        if len(tagged) == 1:
            return tagged[0]
        return exact_tag[0]

    partial = [m for m in models if preferred in m or m.startswith(preferred)]
    if len(partial) == 1:
        return partial[0]
    return None


class OllamaAdapter(OpenAIAdapter):
    """Local Ollama model via OpenAI-compatible API.

    SmolLM2 and many small Ollama models reject native ``tools`` on the API.
    When ``execute_tool`` is provided, this adapter runs a text-based loop:
    parse legacy ``<function=name>{args}</function>`` from model output,
    execute the capability, and re-prompt with the verified result.
    """

    def _build_messages(self, messages, execute_tool=None):
        """Prepend a tool-use reminder to the system prompt when a tool is available."""
        if execute_tool is None:
            return messages
        reminder = (
            "You have access to the runtime tools described below. When current or "
            "external evidence is required, call an offered tool and report only its "
            "result. Prefer native tool calls. If native calls are unavailable, emit "
            "exactly <function=OFFERED_TOOL_NAME>{JSON_ARGUMENTS}</function>. Never "
            "invent tool results or use an unoffered tool name."
        )
        new_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                msg = dict(msg)
                msg["content"] = reminder + "\n\n" + content
            new_messages.append(msg)
        if not any(m.get("role") == "system" for m in new_messages):
            new_messages.insert(0, {"role": "system", "content": reminder})
        return new_messages

    def _extract_tool_call(self, text: str):
        """Extract a legacy tool call from model output.

        Returns (func_name, args_dict) if a call is found, else (None, None).
        """
        import re
        pattern = r'<function=(\w+)>\{([^}]*)\}</function>'
        match = re.search(pattern, text)
        if not match:
            return None, None
        func_name = match.group(1)
        args_str = match.group(2)
        try:
            args = json.loads(args_str) if args_str.strip() else {}
        except json.JSONDecodeError:
            args = {}
        return func_name, args

    def __init__(
        self,
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434/v1",
        think: bool = False,
        timeout: Optional[float] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        prefer_legacy_tools: Optional[bool] = None,
        **kwargs
    ):
        super().__init__(
            model=model,
            api_key="ollama",
            base_url=base_url,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        self.think = think
        self._supports_native_tools: Optional[bool] = None
        if prefer_legacy_tools is None:
            configured = os.environ.get("OLLAMA_PREFER_LEGACY_TOOLS", "1")
            prefer_legacy_tools = configured.strip().lower() not in {
                "0", "false", "no", "off",
            }
        self.prefer_legacy_tools = bool(prefer_legacy_tools)

    def _native_tools_supported(self) -> bool:
        if self.prefer_legacy_tools:
            return False
        if self._supports_native_tools is None:
            capabilities = ollama_model_capabilities(
                self.model,
                base_url=self.base_url,
                timeout=min(self.timeout, 5.0),
            )
            self._supports_native_tools = "tools" in capabilities
        return self._supports_native_tools

    def generate(
        self,
        context: str,
        user_input: str,
        identity: Any,
        think: Optional[bool] = None,
        **kwargs
    ) -> str:
        execute_tool = kwargs.pop("execute_tool", None)
        tools = kwargs.pop("tools", None) or []
        # Ollama's OpenAI-compatible endpoint does not accept native tool
        # selection when tools themselves are handled by the legacy loop.
        kwargs.pop("tool_choice", None)

        extra = dict(kwargs.pop("extra_body", None) or {})
        extra["think"] = self.think if think is None else think

        if execute_tool and tools and self._native_tools_supported():
            output = super().generate(
                context,
                user_input,
                identity,
                tools=tools,
                execute_tool=execute_tool,
                tool_choice="auto",
                extra_body=extra,
                **kwargs,
            )
            text_calls = _parse_all_known_text_tool_calls(output, tools)
            if not text_calls:
                return output
            self._supports_native_tools = False
            evidence_blocks: list[str] = []
            for name, arguments in text_calls:
                try:
                    result = execute_tool(name, arguments)
                except Exception as exc:
                    result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                evidence_blocks.append(f"[Tool `{name}` returned]\n{result}")
                logger.warning(
                    "Recovered Ollama text-form call for offered tool %s; "
                    "continuing with runtime evidence.",
                    name,
                )
            return super().generate(
                context,
                f"{user_input}\n\n" + "\n\n".join(evidence_blocks) + "\n\n"
                "Use only those verified tool results in your answer. Do not "
                "invent content or emit another text-form action.",
                identity,
                extra_body=extra,
                **kwargs,
            )

        if execute_tool and tools:
            context = _legacy_tool_context(context, user_input, tools)
            return self._legacy_tool_loop(
                context,
                user_input,
                identity,
                execute_tool,
                tools=tools,
                extra_body=extra,
                **kwargs,
            )

        return super().generate(
            context,
            user_input,
            identity,
            extra_body=extra,
            **kwargs,
        )

    def _legacy_tool_loop(
        self,
        context: str,
        user_input: str,
        identity: Any,
        execute_tool: Any,
        *,
        tools: list[dict[str, Any]],
        extra_body: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        follow_up = user_input
        for tool_round in range(self.max_tool_rounds + 1):
            text = super().generate(
                context,
                follow_up,
                identity,
                extra_body=extra_body,
                **kwargs,
            )
            calls = _parse_all_known_text_tool_calls(text, tools)
            if not calls:
                return text
            if tool_round >= self.max_tool_rounds:
                raise RuntimeError(
                    f"Adapter tool-call limit reached after {tool_round} round(s); "
                    "refusing to execute an unbounded model tool loop."
                )

            evidence_blocks: list[str] = []
            for name, args in calls:
                try:
                    result = execute_tool(name, args)
                except Exception as exc:
                    result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                evidence_blocks.append(f"[Tool `{name}` returned]\n{result}")
                logger.warning(
                    "Ollama legacy tool loop executed %s; re-prompting model with result.",
                    name,
                )

            follow_up = (
                f"{user_input}\n\n"
                + "\n\n".join(evidence_blocks)
                + "\n\nUse only those verified tool results in your answer. "
                "If another offered tool is required, emit one exact function call; "
                "otherwise answer now."
            )

        raise RuntimeError(
            f"Adapter tool-call limit reached after {self.max_tool_rounds} round(s) "
            "without a final model response."
        )

    def health_check(self) -> bool:
        models = list_ollama_models(base_url=self.base_url, timeout=min(self.timeout, 5.0))
        if not models:
            return False
        if not self.model:
            return True
        return resolve_ollama_model(self.model, models) is not None
