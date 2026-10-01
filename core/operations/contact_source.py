"""Evidence-backed outreach contacts from public GitHub activity.

This source distinguishes a repository or profile that was discovered from a
person who has explicitly published a usable contact address. Candidates are
created only from public profile data or attributed commit metadata.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional
from urllib.parse import quote_plus

from .discovery import Candidate, CandidateSource
from .models import Need

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


class GitHubContactSource(CandidateSource):
    """Find named, publicly contactable maintainers of relevant repositories."""

    name = "github-public-contacts"
    required_skill = "web.fetch"
    _BOT_MARKERS = ("[bot]", "-bot", "noreply", "users.noreply")

    def __init__(
        self,
        repositories: list[str],
        fetch_url: Callable[[str], Optional[str]],
        *,
        search_repositories: bool = True,
        excluded_owners: tuple[str, ...] = ("lacebx",),
        need_categories: Optional[tuple[str, ...]] = None,
        max_repositories: int = 3,
        max_contributors: int = 3,
    ) -> None:
        self._repos = list(repositories)
        self._fetch = fetch_url
        self._search_repositories = search_repositories
        self._excluded_owners = {owner.casefold() for owner in excluded_owners}
        self._need_categories = (
            {category.casefold() for category in need_categories}
            if need_categories is not None
            else None
        )
        self._max_repositories = max_repositories
        self._max_contributors = max_contributors
        self._feed_contact_cache: dict[str, list[tuple[dict[str, str], list[str]]]] = {}

    def _text(self, url: str) -> Optional[str]:
        try:
            return self._fetch(url)
        except Exception:
            return None

    @staticmethod
    def _json(text: Optional[str]) -> object:
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    @classmethod
    def _usable_email(cls, value: object) -> str:
        email = str(value or "").strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            return ""
        if any(marker in email for marker in cls._BOT_MARKERS):
            return ""
        return email

    def parse_contributors(self, json_text: str) -> list[str]:
        rows = self._json(json_text)
        if not isinstance(rows, list):
            return []
        result: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            login = str(row.get("login") or "").strip()
            if not login or any(marker in login.casefold() for marker in self._BOT_MARKERS):
                continue
            result.append(login)
        return result

    def parse_repositories(self, json_text: str) -> list[str]:
        payload = self._json(json_text)
        rows = payload.get("items", []) if isinstance(payload, dict) else []
        result: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("archived") or row.get("fork"):
                continue
            full_name = str(row.get("full_name") or "").strip()
            owner = full_name.partition("/")[0].casefold()
            if "/" not in full_name or owner in self._excluded_owners:
                continue
            result.append(full_name)
        return result

    def parse_profile(self, json_text: str) -> dict[str, str]:
        row = self._json(json_text)
        if not isinstance(row, dict):
            return {}
        email = self._usable_email(row.get("email"))
        if not email:
            return {}
        return {
            "name": str(row.get("name") or row.get("login") or "").strip(),
            "email": email,
            "url": str(row.get("html_url") or "").strip(),
            "organization": str(row.get("company") or "").strip().lstrip("@"),
        }

    def parse_commit_authors(self, json_text: str) -> list[dict[str, str]]:
        rows = self._json(json_text)
        if not isinstance(rows, list):
            return []
        result: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            author = (row.get("commit") or {}).get("author") or {}
            email = self._usable_email(author.get("email"))
            if not email:
                continue
            result.append(
                {
                    "name": str(author.get("name") or "").strip(),
                    "email": email,
                    "url": str(row.get("html_url") or "").strip(),
                }
            )
        return result

    def parse_feed_commits(self, feed_text: str) -> list[str]:
        """Return commit URLs from the public Atom feed, even when truncated."""
        urls = re.findall(
            r'href="(https://github\.com/[^"/]+/[^"/]+/commit/[0-9a-f]+)"',
            feed_text,
            flags=re.IGNORECASE,
        )
        return list(dict.fromkeys(urls))

    def parse_patch_author(self, patch_text: str) -> dict[str, str]:
        match = re.search(r"^From:\s*(.+?)\s*<([^>]+)>\s*$", patch_text, re.MULTILINE)
        if not match:
            return {}
        name = match.group(1).strip().strip('"')
        email = self._usable_email(match.group(2))
        if not email or any(marker in name.casefold() for marker in self._BOT_MARKERS):
            return {}
        return {"name": name, "email": email}

    def _feed_contacts(self, repo: str) -> list[tuple[dict[str, str], list[str]]]:
        if repo in self._feed_contact_cache:
            return self._feed_contact_cache[repo]
        feed = self._text(f"https://github.com/{repo}/commits/HEAD.atom") or ""
        contacts: list[tuple[dict[str, str], list[str]]] = []
        for commit_url in self.parse_feed_commits(feed)[: self._max_contributors]:
            contact = self.parse_patch_author(self._text(f"{commit_url}.patch") or "")
            if not contact:
                continue
            contact["url"] = commit_url
            contacts.append((contact, [f"commit:{commit_url}", "contact:public-patch-author"]))
        self._feed_contact_cache[repo] = contacts
        return contacts

    def _repositories_for(self, need: Need) -> list[str]:
        repositories = list(self._repos)
        if self._search_repositories:
            terms = " ".join(part for part in (need.category, need.description) if part).strip()
            query = quote_plus(f"{terms} in:name,description,topics archived:false")
            text = self._text(
                f"https://api.github.com/search/repositories?q={query}&sort=stars&per_page=2"
            )
            repositories.extend(self.parse_repositories(text or ""))
        unique: list[str] = []
        for repo in repositories:
            owner = repo.partition("/")[0].casefold()
            if "/" not in repo or owner in self._excluded_owners or repo in unique:
                continue
            unique.append(repo)
        return unique[: self._max_repositories]

    def _contact_for(self, repo: str, login: str) -> tuple[dict[str, str], list[str]]:
        profile = self.parse_profile(self._text(f"https://api.github.com/users/{login}") or "")
        if profile:
            return profile, [f"profile:{profile.get('url') or login}"]
        text = self._text(
            f"https://api.github.com/repos/{repo}/commits?author={quote_plus(login)}&per_page=1"
        )
        authors = self.parse_commit_authors(text or "")
        if not authors:
            return {}, []
        author = authors[0]
        return author, [f"commit:{author.get('url') or 'public-metadata'}"]

    def search(self, need: Need) -> list[Candidate]:
        if self._need_categories is not None and need.category.casefold() not in self._need_categories:
            return []
        candidates: list[Candidate] = []
        seen_emails: set[str] = set()
        for repo in self._repositories_for(need):
            contacts = self._feed_contacts(repo)
            if not contacts:
                text = self._text(
                    f"https://api.github.com/repos/{repo}/contributors?per_page={self._max_contributors}"
                )
                contacts = [
                    self._contact_for(repo, login)
                    for login in self.parse_contributors(text or "")[: self._max_contributors]
                ]
            for contact, contact_evidence in contacts:
                email = contact.get("email", "")
                if not email or email in seen_emails:
                    continue
                seen_emails.add(email)
                name = contact.get("name") or email.partition("@")[0]
                candidates.append(
                    Candidate(
                        target_name=name,
                        organization=contact.get("organization") or repo.partition("/")[0],
                        contact_email=email,
                        contact_url=contact.get("url") or f"https://github.com/{login}",
                        category=need.category,
                        relevant_work=[repo],
                        evidence=[
                            f"repo:{repo}",
                            f"contributor:{name}",
                            *contact_evidence,
                        ],
                        fit_reason=(
                            f"Public contributor to {repo}, which is relevant to {need.category}."
                        ),
                        value_proposition=(
                            "Explore shared work on persistent, evidence-backed agent runtimes."
                        ),
                        potential_ask="Would you be open to a brief exchange about complementary work?",
                        confidence=0.7,
                    )
                )
        return candidates

    def find_contacts(self, need: Need) -> list[Candidate]:
        """Compatibility alias for callers introduced during early development."""
        return self.search(need)
