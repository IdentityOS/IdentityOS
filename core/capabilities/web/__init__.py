from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult


@register
class WebCapability(Capability):
    id = "web"
    name = "Web"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Fetch web pages, extract text content, and resolve URLs"
    permissions = ["public"]

    _client: httpx.Client

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._client = httpx.Client(timeout=15, follow_redirects=True)

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.web", {"installed_at": None})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.web")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Web Skills (MANDATORY — use for URL content)",
            "When the user asks you to fetch a URL or check a web page, you MUST use the skills below.",
            "Do NOT say you cannot browse the web. You CAN. Use the skills.",
        ]

    _SKILLS = [
        Skill(name="web.fetch", description="Fetch a URL and return its content as text", permission="public", input_schema=object_schema({"url": {"type": "string", "minLength": 1}}, required=("url",))),
        Skill(name="web.extract", description="Fetch a URL and extract clean text from HTML", permission="public", input_schema=object_schema({"url": {"type": "string", "minLength": 1}}, required=("url",))),
        Skill(name="web.search", description="Search the web and return ranked results (title, url, snippet)", permission="network", input_schema=object_schema({"query": {"type": "string", "minLength": 1}, "limit": {"type": "integer"}}, required=("query",))),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        import time as _time
        _t0 = _time.monotonic()
        try:
            dispatch = {
                "web.fetch": self._fetch,
                "web.extract": self._extract,
                "web.search": self._search,
            }
            handler = dispatch.get(skill_name)
            if handler is None:
                return CapabilityResult.fail("web", skill_name, "unknown_skill", f"Unknown skill: {skill_name}")
            data = handler(**params)
            return CapabilityResult.from_data("web", skill_name, data, source="HTTP fetch", duration_ms=(_time.monotonic() - _t0) * 1000)
        except Exception as e:
            return CapabilityResult.fail("web", skill_name, type(e).__name__, str(e), source="HTTP fetch", duration_ms=(_time.monotonic() - _t0) * 1000)

    def _fetch(self, url: str = "", **kwargs: Any) -> dict[str, Any]:
        if not url:
            return {"error": "url is required"}
        resp = self._client.get(url)
        resp.raise_for_status()
        return {
            "url": url,
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "content_length": len(resp.text),
            "text": resp.text[:5000],
        }

    def _extract(self, url: str = "", **kwargs: Any) -> dict[str, Any]:
        if not url:
            return {"error": "url is required"}
        resp = self._client.get(url)
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r"<script\b[^>]*>.*?</script\b[^>]*>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style\b[^>]*>.*?</style\b[^>]*>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = text[:5000]
        return {
            "url": url,
            "extracted_text": text,
            "character_count": len(text),
        }

    def _search(self, query: str = "", limit: int = 8, **kwargs: Any) -> dict[str, Any]:
        if not query:
            return {"error": "query is required"}
        limit = max(1, min(int(limit or 8), 25))
        resp = self._client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; IdentityOS/2.0)"},
        )
        resp.raise_for_status()
        html = resp.text
        results: list[dict[str, Any]] = []
        pattern = re.compile(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        snippet_pattern = re.compile(
            r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        snippets = [self._clean(m.group("snippet")) for m in snippet_pattern.finditer(html)]
        for index, match in enumerate(pattern.finditer(html)):
            if len(results) >= limit:
                break
            url = self._decode_ddg(match.group("url"))
            results.append({
                "title": self._clean(match.group("title")),
                "url": url,
                "snippet": snippets[index] if index < len(snippets) else "",
            })
        return {"query": query, "results": results, "count": len(results)}

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text or "")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _decode_ddg(url: str) -> str:
        from urllib.parse import parse_qs, unquote, urlparse

        if "uddg=" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "uddg" in params:
                return unquote(params["uddg"][0])
        return url
