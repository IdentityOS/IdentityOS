"""
core/operations/principal_knowledge.py

Grounded public knowledge about the human principal (Arsène Manzi).

Aster should know who she works for: his public GitHub profile, his
portfolio site, and his public repositories — fetched from public sources,
cached durably, refreshed weekly, and cited as verified facts in replies.
This is broad reasoning from evidence, not hard-coded praise: every line
the model may cite traces to a fetched source with a timestamp, and unknown
things stay unknown (never invented).

Only PUBLIC sources are ever fetched. Nothing private, no credentials, no
authenticated endpoints. The profile lives in ordinary identity state
(``operations.principal_profile``), never in git.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Optional

logger = logging.getLogger("identityos.principal_knowledge")

PROFILE_NAMESPACE = "operations.principal_profile"
REFRESH_INTERVAL = timedelta(days=7)
MAX_REPOS = 6
MAX_TEXT_CHARS = 1500


@dataclass
class PrincipalProfile:
    github_user: str = ""
    github_name: str = ""
    github_bio: str = ""
    github_blog: str = ""
    public_repos: int = 0
    top_repos: list[dict[str, str]] = field(default_factory=list)
    portfolio_text: str = ""
    portfolio_url: str = ""
    fetched_at: str = ""
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "github_user": self.github_user,
            "github_name": self.github_name,
            "github_bio": self.github_bio,
            "github_blog": self.github_blog,
            "public_repos": self.public_repos,
            "top_repos": list(self.top_repos),
            "portfolio_text": self.portfolio_text,
            "portfolio_url": self.portfolio_url,
            "fetched_at": self.fetched_at,
            "sources": list(self.sources),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrincipalProfile":
        data = data or {}
        return cls(
            github_user=str(data.get("github_user", "")),
            github_name=str(data.get("github_name", "")),
            github_bio=str(data.get("bio", data.get("github_bio", ""))),
            github_blog=str(data.get("blog", data.get("github_blog", ""))),
            public_repos=int(data.get("public_repos", 0) or 0),
            top_repos=[r for r in (data.get("top_repos") or []) if isinstance(r, dict)][:MAX_REPOS],
            portfolio_text=str(data.get("portfolio_text", ""))[:MAX_TEXT_CHARS],
            portfolio_url=str(data.get("portfolio_url", "")),
            fetched_at=str(data.get("fetched_at", "")),
            sources=[str(s) for s in (data.get("sources") or [])][:8],
        )

    def is_fresh(self, *, now: Optional[datetime] = None) -> bool:
        if not self.fetched_at:
            return False
        try:
            fetched = datetime.fromisoformat(self.fetched_at)
        except ValueError:
            return False
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - fetched) < REFRESH_INTERVAL

    def is_empty(self) -> bool:
        return not (self.github_bio or self.github_name or self.top_repos or self.portfolio_text)


def derive_github_user(project_url: str = "https://github.com/lacebx/IdentityOS") -> str:
    """Derive the principal's GitHub username from the project's repo URL."""
    match = re.search(r"github\.com/([A-Za-z0-9-]+)", project_url or "")
    return match.group(1) if match else ""


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\(?\+?[\d][\d\s().\-]*\d\)?")


def _scrub_contacts(text: str) -> str:
    """Redact emails and phone numbers from fetched public text.

    The profile exists so Aster knows her principal's WORK, never to
    circulate his contact details: a redacted model context cannot leak
    what it was never given. Dates and versions are preserved (digit-count
    and separator rules exclude them).
    """

    def _phone_sub(match: re.Match) -> str:
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        separators = set(candidate) - set("0123456789")
        if len(digits) >= 9 and (separators & {"+", "(", ")", " "}):
            return "[contact redacted]"
        if len(digits) >= 10 and len(candidate) >= 12:
            return "[contact redacted]"
        return candidate

    text = _EMAIL_RE.sub("[contact redacted]", text or "")
    return _PHONE_RE.sub(_phone_sub, text)


def _clean_html_text(html: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    if not html:
        return ""

    class _Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._parts: list[str] = []
            self._skip = False

        def handle_starttag(self, tag: Any, attrs: Any) -> None:
            tag = str(tag).lower()
            if tag in ("script", "style", "nav", "header", "footer"):
                self._skip = True

        def handle_endtag(self, tag: Any) -> None:
            if str(tag).lower() in ("script", "style", "nav", "header", "footer"):
                self._skip = False

        def handle_data(self, data: Any) -> None:
            if not self._skip and str(data or "").strip():
                self._parts.append(str(data).strip())

    parser = _Extractor()
    try:
        parser.feed(html[:20000])
    except Exception:
        pass
    text = re.sub(r"\s+", " ", " ".join(parser._parts)).strip()
    return text[:limit]


_REPO_FIELD_RES = {
    "name": re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.){1,100})"'),
    "description": re.compile(r'"description"\s*:\s*(?:"((?:[^"\\]|\\.){0,200})"|null)'),
    "language": re.compile(r'"language"\s*:\s*(?:"([^"]{1,40})"|null)'),
    "stars": re.compile(r'"stargazers_count"\s*:\s*(\d+)'),
}


def _extract_repo_entries(raw: str) -> list[dict[str, str]]:
    """Tolerantly parse repo entries from possibly-truncated API JSON.

    The web capability caps response text, so full-array parsing routinely
    fails; per-object field extraction degrades gracefully instead.
    """
    entries: list[dict[str, str]] = []
    for chunk in re.split(r"\{[ \t\r\n]*\"id\"\s*:", raw or "")[1:]:
        chunk = chunk[:4000]
        name_match = _REPO_FIELD_RES["name"].search(chunk)
        if not name_match:
            continue
        description_match = _REPO_FIELD_RES["description"].search(chunk)
        language_match = _REPO_FIELD_RES["language"].search(chunk)
        stars_match = _REPO_FIELD_RES["stars"].search(chunk)
        try:
            stars = int(stars_match.group(1)) if stars_match else 0
        except (TypeError, ValueError):
            stars = 0
        entries.append({
            "name": name_match.group(1),
            "description": (description_match.group(1) or "") if description_match else "",
            "language": language_match.group(1) if language_match else "",
            "stars": str(stars),
        })
    return entries


def fetch_principal_profile(
    fetcher: Callable[[str], str],
    *,
    github_user: str,
    portfolio_url: str = "",
    extractor: Optional[Callable[[str], str]] = None,
) -> PrincipalProfile:
    """Gather public principal facts. Partial results are fine; never raises."""
    profile = PrincipalProfile(github_user=github_user)
    if not github_user:
        return profile

    def _get(url: str) -> str:
        try:
            return fetcher(url) or ""
        except Exception as exc:
            logger.warning("principal fetch failed for %s: %s", url, exc)
            return ""

    def _extract(url: str) -> str:
        if extractor is None:
            return ""
        try:
            return extractor(url) or ""
        except Exception as exc:
            logger.warning("principal extract failed for %s: %s", url, exc)
            return ""

    user_raw = _get(f"https://api.github.com/users/{github_user}")
    if user_raw:
        try:
            user = json.loads(user_raw)
            if isinstance(user, dict) and not user.get("message"):
                profile.github_name = str(user.get("name") or "")
                profile.github_bio = _scrub_contacts(str(user.get("bio") or ""))
                profile.github_blog = _scrub_contacts(str(user.get("blog") or ""))
                try:
                    profile.public_repos = int(user.get("public_repos") or 0)
                except (TypeError, ValueError):
                    pass
                profile.sources.append(f"https://api.github.com/users/{github_user}")
        except (ValueError, AttributeError):
            pass

    repos_raw = _get(f"https://api.github.com/users/{github_user}/repos?sort=updated&per_page=100")
    entries = _extract_repo_entries(repos_raw)
    if entries:
        # The API returns most-recently-pushed first; rank by stars for the
        # most notable work, keep it deterministic.
        entries.sort(key=lambda entry: (int(entry["stars"] or 0), entry["name"]), reverse=True)
        profile.top_repos = entries[:MAX_REPOS]
        profile.sources.append("github-repos")

    portfolio = portfolio_url or f"https://{github_user}.github.io"
    extracted = _extract(portfolio)
    if extracted and len(extracted) > 120:
        profile.portfolio_text = _scrub_contacts(extracted[:MAX_TEXT_CHARS])
        profile.portfolio_url = portfolio
        profile.sources.append(portfolio)
    else:
        page = _get(portfolio)
        if page and len(page) > 500:
            cleaned = _scrub_contacts(_clean_html_text(page))
            if len(cleaned) > 120:
                profile.portfolio_text = cleaned
                profile.portfolio_url = portfolio
                profile.sources.append(portfolio)

    profile.fetched_at = datetime.now(timezone.utc).isoformat()
    return profile


def load_profile(storage: Any, identity_id: str) -> PrincipalProfile:
    try:
        raw = storage.load(identity_id, PROFILE_NAMESPACE) or {}
    except Exception:
        raw = {}
    return PrincipalProfile.from_dict(raw.get("profile", raw) if isinstance(raw, dict) else {})


def save_profile(storage: Any, identity_id: str, profile: PrincipalProfile) -> None:
    storage.save(identity_id, PROFILE_NAMESPACE, {"profile": profile.to_dict()})


def principal_context_lines(
    profile: PrincipalProfile,
    *,
    limit: int = 8,
    founder: str = "",
    project: str = "",
) -> list[str]:
    """Verified principal facts formatted for model context."""
    if profile.is_empty() and not founder:
        return []
    lines: list[str] = []
    if founder:
        lines.append(
            f"{founder} is the founder of the {project or 'IdentityOS'} project "
            f"and Aster's human principal and builder."
        )
    who = profile.github_name or profile.github_user
    if who:
        lines.append(f"Arsène Manzi's GitHub identity is '{profile.github_user}'"
                     + (f" (display name '{profile.github_name}')" if profile.github_name else "")
                     + ".")
    if profile.github_bio:
        lines.append(f"His public GitHub bio states: {profile.github_bio[:220]}")
    if profile.github_blog:
        lines.append(f"His listed site is {profile.github_blog[:120]}.")
    for repo in profile.top_repos[:3]:
        detail = f"{repo['name']}"
        if repo.get("description"):
            detail += f": {repo['description'][:120]}"
        if repo.get("language"):
            detail += f" [{repo['language']}]"
        lines.append(f"He maintains the public repository {detail}.")
    if profile.portfolio_text:
        lines.append(f"His portfolio site says: {profile.portfolio_text[:400]}")
    lines.append(f"These facts were fetched from public sources on {profile.fetched_at[:10]}; "
                 "cite only what is written here, never invent achievements.")
    return lines[: max(1, limit)]
