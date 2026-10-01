from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AdapterMessage:
    """A single message in a conversation format."""
    role: str   # "system", "user", "assistant"
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterRequest:
    """The fully assembled request sent to an LLM adapter."""
    messages: List[AdapterMessage]
    identity_id: str = ""
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 1024
    stream: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResponse:
    """The response returned from an LLM adapter."""
    content: str
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)  # prompt/completion/total tokens
    finish_reason: str = "stop"
    metadata: Dict[str, Any] = field(default_factory=dict)


MAX_INCREMENTING_KEYS = 16


def collect_api_keys(env_prefix: str) -> List[str]:
    """Collect ``{prefix}``, ``{prefix}_2``, ``{prefix}_3`` ... keys from env.

    Scans incrementally so adding a new numbered key (e.g. API_KEY_4) requires
    no code change. Stops scanning after the first gap or ``MAX_INCREMENTING_KEYS``.
    Empty / placeholder values are skipped.
    """
    keys: List[str] = []
    seen: set = set()
    for idx in range(1, MAX_INCREMENTING_KEYS + 1):
        var = env_prefix if idx == 1 else f"{env_prefix}_{idx}"
        val = os.environ.get(var)
        if not val or not val.strip():
            break
        val = val.strip()
        if "PLACEHOLDER" in val or val in seen:
            continue
        seen.add(val)
        keys.append(val)
    return keys


class BaseAdapter(ABC):
    """
    Abstract base class for all LLM adapters.

    An adapter is a thin translation layer between the IdentityRuntime
    and a specific LLM provider (OpenAI, Anthropic, Ollama, etc.).

    Adapters receive:
    - A composed context string (system prompt + identity context)
    - The user input
    - The active identity

    Adapters return raw string output. Post-processing is handled by the runtime.

    Design principle: adapters are DUMB. They translate and call. No logic.
    """

    def __init__(self, model: str = "", **kwargs):
        self.model = model
        self.config = kwargs

    @property
    def structured_output(self) -> str:
        """prompt_only, json_object or json_schema; never a fact-verification guarantee."""
        return "prompt_only"

    @abstractmethod
    def generate(
        self,
        context: str,
        user_input: str,
        identity: Any,
        **kwargs
    ) -> str:
        """
        Generate a response from the LLM.

        Args:
            context: The rendered system context from ContextComposer.
            user_input: The sanitized user input.
            identity: The active Identity object.

        Kwargs:
            tools: Optional list of OpenAI-compatible tool definitions.
            execute_tool: Optional callable(name, args) -> str to run tool calls.

        Returns:
            Raw string output from the LLM.
        """
        ...

    def build_messages(
        self, context: str, user_input: str
    ) -> List[AdapterMessage]:
        """Helper to build a standard message list."""
        return [
            AdapterMessage(role="system", content=context),
            AdapterMessage(role="user", content=user_input),
        ]

    def health_check(self) -> bool:
        """Optional: verify the adapter can reach its backend."""
        return True

    def generate_with_vision(
        self,
        context: str,
        user_input: str,
        identity: Any,
        image_base64: str,
        **kwargs
    ) -> str:
        """
        Generate a response from the LLM with vision (image) input.

        Args:
            context: The rendered system context from ContextComposer.
            user_input: The sanitized user input (instruction).
            identity: The active Identity object.
            image_base64: Base64-encoded image data.

        Kwargs:
            tools: Optional list of OpenAI-compatible tool definitions.
            execute_tool: Optional callable(name, args) -> str to run tool calls.

        Returns:
            Raw string output from the LLM.

        Default implementation raises NotImplementedError.
        Adapters that support vision should override this method.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support vision")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(model={self.model!r})"
