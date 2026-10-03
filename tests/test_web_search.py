from __future__ import annotations

from core.capabilities.web import WebCapability
from core.capabilities.registry import CapabilityRegistry
from runtime.persistence import InMemoryBackend


class _FakeResponse:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _fake_client(html: str):
    class _Client:
        def __init__(self, target_html: str) -> None:
            self.target_html = target_html

        def get(self, url, params=None, headers=None):
            return _FakeResponse(self.target_html)

    return _Client(html)


_SEARCH_HTML = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ffund">Example Funding Program</a>
<a class="result__snippet">A grant program for open identity infrastructure.</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fcompute">Compute Credits</a>
<a class="result__snippet">Free GPU credits for open source maintainers.</a>
</body></html>
"""


def test_web_search_parses_results():
    cap = WebCapability()
    cap._client = _fake_client(_SEARCH_HTML)  # type: ignore[assignment]
    result = cap.call("web.search", query="funding program identity")
    assert result.success is True
    assert result.data["count"] == 2
    first = result.data["results"][0]
    assert first["url"] == "https://example.org/fund"
    assert "Funding Program" in first["title"]
    assert "grant program" in first["snippet"]


def test_web_search_respects_limit():
    cap = WebCapability()
    cap._client = _fake_client(_SEARCH_HTML)  # type: ignore[assignment]
    result = cap.call("web.search", query="funding", limit=1)
    assert len(result.data["results"]) == 1


def test_web_search_requires_grant_in_registry():
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "web")
    allowed, reason = registry.can("aster", "web.search")
    assert allowed is False
    registry.grant("aster", "web", "network")
    allowed, _ = registry.can("aster", "web.search")
    assert allowed is True