from __future__ import annotations

import collections
import re
from typing import Any, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult


@register
class TextCapability(Capability):
    inspection_readiness = "ready"
    id = "text"
    name = "Text Processing"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Count words, extract keywords, split text, detect patterns"
    permissions = ["public"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.text", {"installed_at": None})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.text")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Text Processing Skills (MANDATORY — use for text analysis)",
            "When the user asks you to analyze, count, or extract from text, you MUST use the skills below.",
            "Do NOT estimate word counts or character counts — use the text.stats skill precisely.",
        ]

    _SKILLS = [
        Skill(name="text.stats", description="Return word count, character count, line count, and estimated reading time", permission="public", input_schema=object_schema({"text": {"type": "string"}}, required=("text",)), verification_params={"text": "verification text"}),
        Skill(name="text.keywords", description="Extract most frequent words from text (excluding common stop words)", permission="public", input_schema=object_schema({"text": {"type": "string"}, "top_n": {"type": "integer", "minimum": 1, "maximum": 100}}, required=("text",))),
        Skill(name="text.extract_pattern", description="Extract URLs, emails, or hashtags from text", permission="public", input_schema=object_schema({"text": {"type": "string"}, "pattern": {"type": "string", "enum": ["urls", "emails", "hashtags", "mentions"]}}, required=("text", "pattern"))),
        Skill(name="text.split", description="Split text into chunks by token count or paragraph", permission="public", input_schema=object_schema({"text": {"type": "string"}, "method": {"type": "string", "enum": ["paragraph", "tokens", "lines"]}, "chunk_size": {"type": "integer", "minimum": 1, "maximum": 100000}}, required=("text",))),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        import time as _time
        _t0 = _time.monotonic()
        try:
            dispatch = {
                "text.stats": self._stats,
                "text.keywords": self._keywords,
                "text.extract_pattern": self._extract_pattern,
                "text.split": self._split,
            }
            handler = dispatch.get(skill_name)
            if handler is None:
                return CapabilityResult.fail("text", skill_name, "unknown_skill", f"Unknown skill: {skill_name}")
            data = handler(**params)
            return CapabilityResult.from_data("text", skill_name, data, source="text processor", duration_ms=(_time.monotonic() - _t0) * 1000)
        except Exception as e:
            return CapabilityResult.fail("text", skill_name, type(e).__name__, str(e), duration_ms=(_time.monotonic() - _t0) * 1000)

    @staticmethod
    def _stats(text: str = "", **kwargs: Any) -> dict[str, Any]:
        words = text.split()
        chars = len(text)
        lines = text.count("\n") + 1 if text else 0
        reading_time_min = max(1, round(len(words) / 200))
        return {
            "word_count": len(words),
            "character_count": chars,
            "line_count": lines,
            "estimated_reading_time_minutes": reading_time_min,
        }

    @staticmethod
    def _keywords(text: str = "", top_n: int = 10, **kwargs: Any) -> dict[str, Any]:
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "shall", "can", "need", "dare",
            "it", "its", "this", "that", "these", "those", "i", "you", "he",
            "she", "we", "they", "me", "him", "her", "us", "them", "my", "your",
            "his", "their", "our", "not", "no", "nor", "so", "as", "if", "than",
            "then", "just", "about", "also", "very", "too", "really", "more",
        }
        words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        counter = collections.Counter(w for w in words if w not in stop_words)
        return {"keywords": counter.most_common(top_n)}

    @staticmethod
    def _extract_pattern(text: str = "", pattern: str = "urls", **kwargs: Any) -> dict[str, Any]:
        patterns = {
            "urls": r"https?://[^\s<>\"']+|www\.[^\s<>\"']+",
            "emails": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            "hashtags": r"#\w+",
            "mentions": r"@\w+",
        }
        regex = patterns.get(pattern)
        if regex is None:
            return {"error": f"Unknown pattern: {pattern}. Available: {', '.join(patterns.keys())}"}
        matches = re.findall(regex, text)
        return {"pattern": pattern, "matches": matches, "count": len(matches)}

    @staticmethod
    def _split(text: str = "", method: str = "paragraph", chunk_size: int = 500, **kwargs: Any) -> dict[str, Any]:
        chunks = []
        if method == "paragraph":
            chunks = [p.strip() for p in text.split("\n\n") if p.strip()]
        elif method == "tokens":
            words = text.split()
            for i in range(0, len(words), chunk_size):
                chunks.append(" ".join(words[i:i + chunk_size]))
        elif method == "lines":
            lines = text.split("\n")
            for i in range(0, len(lines), max(1, chunk_size)):
                chunks.append("\n".join(lines[i:i + chunk_size]))
        else:
            return {"error": f"Unknown method: {method}. Available: paragraph, tokens, lines"}
        return {"method": method, "chunks": len(chunks), "chunk_size": chunk_size, "total_characters": len(text)}
