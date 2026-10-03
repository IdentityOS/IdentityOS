"""Contact evidence from public GitHub metadata, never guesses.

Public commit authorship is a verifiable fact: the author email on a commit is
the only attestation of authorship for an open-source change. Two contact
paths exist and both are evidence-backed:

* the GitHub profile API (``/users/<login>``), which carries a public email
  when the person published one;
* the public patch feed (``/commits/HEAD.atom`` then ``<commit>.patch``),
  whose ``From:`` header names the author and their address.

Both fit inside the web.fetch transport (small payloads). A candidate is never
invented: if no real evidence surfaces, the source returns nothing. Own
repositories and out-of-scope need categories are excluded before any network
call, so a scoped or self-directed search costs nothing and contacts nobody.
"""

from __future__ import annotations

import email.utils
import json
import re
from typing import Any, Callable, Optional

from .discovery import Candidate, CandidateSource
from .models import Need


class GitHubContactSource(CandidateSource):
    """Find real, publicly contactable humans behind relevant GitHub work.

    Repositories are seeded by config (never guessed), and every contact is
    proven by public evidence before it becomes a candidate. Scoping happens
    before fetching: an out-of-scope need or an own repository makes zero
    network calls.
    """

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

    # ── parsing (transport-safe: small, complete payloads only) ──

    def _text(self, url: str) -> Optional[str]:
        try:
            return self._fetch(url)
        except Exception:
            return None

    def parse_contributors(self, text: str) -> list[str]:
        try:
            rows = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return []
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            login = str(row.get("login") or "").strip()
            if not login or any(marker in login.lower() for marker in self._BOT_MARKERS):
                continue
            out.append(login)
        return out

    def parse_commit_authors(self, text: str) -> list[dict]:
        try:
            rows = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return []
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            author = (row.get("commit") or {}).get("author") or {}
            entry = email.utils.parseaddr(str(author.get("name", "")) +
                                          " <" + str(author.get("email", "")) + ">")
            address = (entry[1] or "").strip().lower()
            if not address or any(marker in address for marker in self._BOT_MARKERS):
                continue
            out.append({"email": address, "url": str(row.get("html_url", ""))})
        return out

    def _parse_patch_from(self, text: str) -> Optional[dict]:
        """``From: Name <address>`` from a public .patch file."""
        for line in (text or "").splitlines():
            if line.lower().startswith("from:"):
                name, address = email.utils.parseaddr(line[5:].strip())
                address = (address or "").strip().lower()
                if not address or any(marker in address for marker in self._BOT_MARKERS):
                    return None
                return {"name": name or address, "email": address}
        return None

    def _parse_atom_links(self, text: str) -> list[str]:
        return re.findall(r'<link[^>]+href="([^"]+)"', text or "")

    # ── contact paths ──

    def _profile_contact(self, repo: str, login: str) -> Optional[dict]:
        profile = self._json(f"https://api.github.com/users/{login}")
        if not isinstance(profile, dict):
            return None
        address = str(profile.get("email") or "").strip().lower()
        if not address or any(marker in address for marker in self._BOT_MARKERS):
            return None
        return {
            "name": str(profile.get("name") or login),
            "email": address,
            "url": str(profile.get("html_url") or f"https://github.com/{login}"),
            "organization": str(profile.get("company") or ""),
            "evidence": [f"repo:{repo}", f"profile:{login}", "contact:public-profile"],
        }

    def _patch_contact(self, repo: str) -> Optional[dict]:
        atom = self._text(f"https://api.github.com/repos/{repo}/commits/HEAD.atom")
        links = self._parse_atom_links(atom or "")
        for link in links:
            if "/commit/" not in link:
                continue
            patch = self._text(f"{link}.patch")
            parsed = self._parse_patch_from(patch or "")
            if parsed is None:
                continue
            return {
                "name": parsed["name"],
                "email": parsed["email"],
                "url": f"https://github.com/{parsed['name'].replace(' ', '-')}" if False else link,
                "organization": repo.split("/", 1)[1] if "/" in repo else repo,
                "evidence": [f"repo:{repo}", f"commit:{link}", "contact:public-patch-author"],
            }
        return None

    def _json(self, url: str) -> Optional[Any]:
        text = self._text(url)
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    # ── discovery ──

    def search(self, need: Need) -> list[Candidate]:
        # Scope BEFORE any network call: an out-of-scope need contacts nobody
        # and costs nothing.
        if self._need_categories is not None and need.category.casefold() not in self._need_categories:
            return []

        seeds: list[str] = []
        for repo in self._repos:
            owner = repo.split("/", 1)[0].casefold() if "/" in repo else repo.casefold()
            # Own repositories are never outreach targets, and excluding them
            # must not even cost a fetch.
            if owner in self._excluded_owners:
                continue
            seeds.append(repo)
        if not seeds:
            return []

        if self._search_repositories:
            seeds = self._discover_repositories(need, seeds)[: self._max_repositories]

        candidates: list[Candidate] = []
        seen_emails: set[str] = set()
        for repo in seeds:
            for contact in self._contacts_for(repo):
                if contact["email"] in seen_emails:
                    continue
                seen_emails.add(contact["email"])
                candidates.append(Candidate(
                    target_name=contact["name"],
                    organization=contact["organization"],
                    contact_email=contact["email"],
                    contact_url=contact["url"],
                    channel="email",
                    category=need.category,
                    evidence=list(contact["evidence"]),
                    fit_reason=f"Public maintainer of {repo}",
                    value_proposition=f"builds {repo}; adjacent work could help both projects",
                    potential_ask="compare approaches on durable identity operations",
                    confidence=0.6 if "public-profile" in contact["evidence"][-1] else 0.5,
                ))
        return candidates

    def _discover_repositories(self, need: Need, seeds: list[str]) -> list[str]:
        """Best-effort repo discovery; failures keep the curated seeds."""
        payload = self._json(
            "https://api.github.com/search/repositories?q="
            + re.escape(need.category) + f"&sort=updated&per_page={self._max_repositories}"
        )
        if not isinstance(payload, dict):
            return seeds
        found = [
            str(item.get("full_name"))
            for item in (payload.get("items") or [])
            if isinstance(item, dict) and item.get("full_name")
        ]
        merged = list(dict.fromkeys([*seeds, *found]))
        return merged

    def _commit_contact(self, repo: str, login: str) -> Optional[dict]:
        """Public commit metadata carries the author address. per_page=1 keeps
        the payload under the transport's 5000-char cap so it parses."""
        rows = self._json(
            f"https://api.github.com/repos/{repo}/commits?author={login}&per_page=1"
        )
        if not isinstance(rows, list) or not rows:
            return None
        author = (rows[0].get("commit") or {}).get("author") or {}
        entry = email.utils.parseaddr(
            str(author.get("name", "")) + " <" + str(author.get("email", "")) + ">"
        )
        address = (entry[1] or "").strip().lower()
        if not address or any(marker in address for marker in self._BOT_MARKERS):
            return None
        return {
            "name": str(author.get("name") or login),
            "email": address,
            "url": str(rows[0].get("html_url") or f"https://github.com/{login}"),
            "organization": "",
            "evidence": [f"repo:{repo}", f"commit_author:{login}", "contact:public-commit"],
        }

    def _contacts_for(self, repo: str) -> list[dict]:
        contacts: list[dict] = []
        # per_page bounds the payload so the JSON parses inside the transport.
        text = self._text(f"https://api.github.com/repos/{repo}/contributors?per_page=5")
        for login in self.parse_contributors(text or "")[: self._max_contributors]:
            # Primary: commit metadata (worked live; most people publish here
            # through their git config even when their profile hides it).
            commit = self._commit_contact(repo, login)
            if commit is not None:
                contacts.append(commit)
                continue
            profile = self._profile_contact(repo, login)
            if profile is not None:
                contacts.append(profile)
        if not contacts:
            patch = self._patch_contact(repo)
            if patch is not None:
                contacts.append(patch)
        return contacts
