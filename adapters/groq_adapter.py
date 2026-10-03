from __future__ import annotations

import logging
import os
import time
from typing import Any, List, Optional

from .base import collect_api_keys
from .openai_adapter import OpenAIAdapter

logger = logging.getLogger(__name__)


class GroqAdapter(OpenAIAdapter):
    """
    Adapter for Groq with automatic API key rotation on rate limits.

    Supports any number of API keys via environment variables:
        GROQ_API_KEY    — primary key
        GROQ_API_KEY_2  — first fallback
        GROQ_API_KEY_3  — second fallback
        ...             — further numbered keys are auto-discovered

    When one key hits a 429 / rate-limit error, the adapter waits
    for the retry-after window, then rotates to the next key.
    If all keys are rate-limited simultaneously, it waits for
    the shortest retry-after and retries.
    """

    def __init__(
        self,
        model: str = "openai/gpt-oss-120b",
        api_key: Optional[str] = None,
        base_url: str = "https://api.groq.com/openai/v1",
        api_keys: Optional[List[str]] = None,
        **kwargs: Any,
    ):
        base_url = os.environ.get("GROQ_BASE_URL", base_url)

        # Collect all available keys — auto-increments API_KEY_2..API_KEY_N
        self._keys: List[str] = api_keys or collect_api_keys("GROQ_API_KEY")
        if api_key and api_key not in self._keys:
            self._keys.insert(0, api_key)

        if not self._keys:
            logger.warning("No valid Groq API keys found")

        # Track cooldowns per key index: {index: unix_ts_until}
        self._cooldowns: dict = {}
        self._key_index = 0

        # Only wait this long for the shortest cooldown; beyond that, give up
        # so the chain falls through to another provider promptly.
        self._MAX_COOLDOWN_WAIT = 15.0

        # Initialize with the first key
        current_key = self._keys[0] if self._keys else api_key
        super().__init__(
            model=model,
            api_key=current_key,
            base_url=base_url,
            **kwargs,
        )

    def _current_key(self) -> str:
        if not self._keys:
            return self.api_key or ""
        return self._keys[self._key_index]

    def _rotate_key(self) -> Optional[str]:
        """Move to the next key not in cooldown. Returns None if all are on cooldown."""
        now = time.time()
        for _ in range(len(self._keys)):
            self._key_index = (self._key_index + 1) % len(self._keys)
            cooldown_until = self._cooldowns.get(self._key_index, 0)
            if cooldown_until <= now:
                logger.info("Rotated to Groq API key index %d", self._key_index)
                self.api_key = self._keys[self._key_index]
                self._client = None
                return self.api_key
        return None

    def _wait_shortest_cooldown(self, retry_after: float = 60) -> bool:
        """Wait briefly for the shortest cooldown to expire.

        Returns ``True`` when a key should be retryable after the wait;
        returns ``False`` when every key is on a long cooldown, signalling
        the caller to give up so the ChainAdapter can fall through to the
        next provider instead of blocking the chat.
        """
        now = time.time()
        min_wait = retry_after
        for idx, until in self._cooldowns.items():
            remaining = until - now
            if 0 < remaining < min_wait:
                min_wait = remaining
        if min_wait <= 0:
            return True
        if min_wait > self._MAX_COOLDOWN_WAIT:
            logger.warning(
                "All keys on cooldown (shortest %.0fs). Falling through.",
                min_wait,
            )
            return False
        logger.warning("All keys on cooldown. Waiting %.0fs...", min_wait)
        import math
        time.sleep(math.ceil(min_wait) + 1)
        # Rotate to the first available key
        self._key_index = 0
        self._rotate_key()
        return True

    def _extract_retry_after(self, error_msg: str) -> float:
        """Parse retry-after duration from a Groq 429 error message.

        Handles the formats Groq actually sends:
          "try again in 55m49s"   -> 3349s
          "try again in 55m 49s"  -> 3349s
          "try again in 1m0s"     -> 60s
          "try again in 60.0s"    -> 60s  (not 3600s!)
          "try again in 55.8m"    -> 3348s
        """
        import re
        msg = error_msg.lower()
        # minutes + seconds: "55m49s" / "55m 49s"
        m = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*m(?:in)?\s*(\d+(?:\.\d+)?)\s*s", msg)
        if m:
            return float(m.group(1)) * 60 + float(m.group(2))
        # minutes only: "55.8m" / "55m"
        m = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*m(?:in)?\b", msg)
        if m:
            return float(m.group(1)) * 60
        # seconds only: "60.0s" / "60s" / "30 seconds"
        m = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*s", msg)
        if m:
            return float(m.group(1))
        return 60  # Default fallback

    def _request_deadline_seconds(self) -> float:
        """Bound one call while honoring an explicitly enlarged cooldown wait."""
        return max(45.0, self._MAX_COOLDOWN_WAIT + 15.0)

    @property
    def structured_output(self):
        # Groq documents strict output for these models; tools remain prompt-only.
        return "json_schema" if self.model in {"openai/gpt-oss-120b", "openai/gpt-oss-20b"} else "json_object"

    def generate(
        self,
        context: str,
        user_input: str,
        identity: Any,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        # Contract requests use one bounded attempt per provider, without key-rotation replay.
        if "_generation_budget" in kwargs:
            return super().generate(context, user_input, identity, temperature=temperature,
                                    max_tokens=max_tokens, retries=1, **kwargs)
        last_error = None
        now = time.time()
        # Interactive callers keep the default 45-second bound.  A benchmark
        # may explicitly raise _MAX_COOLDOWN_WAIT after moving work off the
        # chat path; its overall deadline must then leave room for that wait to
        # have an effect instead of timing out immediately after sleeping.
        deadline = now + self._request_deadline_seconds()

        for attempt in range(len(self._keys) * 3):
            if time.time() > deadline:
                raise RuntimeError(
                    f"All Groq API keys exhausted (timeout after 45s). Last error: {last_error}"
                ) from last_error
            # Skip keys on cooldown
            cooldown_until = self._cooldowns.get(self._key_index, 0)
            if cooldown_until > now:
                if self._rotate_key() is None:
                    if not self._wait_shortest_cooldown():
                        raise RuntimeError(
                            "All Groq API keys on cooldown. Last error: "
                            f"{last_error}"
                        ) from last_error
                    now = time.time()

            try:
                remaining = max(1.0, deadline - time.time())
                return super().generate(
                    context=context,
                    user_input=user_input,
                    identity=identity,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    retries=1,  # Parent retry disabled — GroqAdapter handles rotation
                    timeout=remaining,
                    **kwargs,
                )
            except RuntimeError as exc:
                last_error = exc
                msg = str(exc)
                msg_lower = msg.lower()
                # 413 = request too large (model context window exceeded) — NOT a rate limit
                # Treat as exhaustion so chain falls through to a larger-model adapter
                if "413" in msg_lower or "request too large" in msg_lower:
                    raise RuntimeError(
                        f"All Groq API keys exhausted (context too large). Last error: {last_error}"
                    ) from last_error
                if "429" in msg_lower or "rate limit" in msg_lower or "quota" in msg_lower or "insufficient_quota" in msg_lower:
                    retry_after = self._extract_retry_after(msg)
                    logger.warning(
                        "Rate limited on key %s, cooldown %.0fs",
                        self._key_index,
                        retry_after,
                    )
                    self._cooldowns[self._key_index] = time.time() + retry_after
                    if self._rotate_key() is None:
                        if not self._wait_shortest_cooldown(retry_after):
                            raise RuntimeError(
                                "All Groq API keys on cooldown. Last error: "
                                f"{last_error}"
                            ) from last_error
                    continue
                if (
                    "401" in msg_lower
                    or "invalid api key" in msg_lower
                    or "invalid_api_key" in msg_lower
                    or "authentication_error" in msg_lower
                ):
                    # A stale key must not prevent configured fallbacks from
                    # being tried. Quarantine it for this adapter instance;
                    # unlike a rate limit, invalid credentials do not recover
                    # after a short wait.
                    rejected_index = self._key_index
                    self._cooldowns[rejected_index] = float("inf")
                    logger.warning(
                        "Groq rejected API key index %d; disabling it for this process.",
                        rejected_index,
                    )
                    if self._rotate_key() is None:
                        raise RuntimeError(
                            "All Groq API keys were rejected or are unavailable. "
                            f"Last error: {last_error}"
                        ) from last_error
                    continue
                raise  # Non-retryable error

        raise RuntimeError(
            f"All Groq API keys exhausted. Last error: {last_error}"
        ) from last_error
