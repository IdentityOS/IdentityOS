"""Authority is supplied by trusted adapters, never parsed from model text."""

from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from functools import wraps
from time import monotonic


class AuthLevel(IntEnum):
    ANONYMOUS = 0
    RECOGNIZED = 1
    PIN = 2


@dataclass(frozen=True)
class ChannelContext:
    channel: str
    transport: str
    interface: str
    caller_id: str
    call_id: str
    auth_level: AuthLevel = AuthLevel.ANONYMOUS
    screen_available: bool = False
    keyboard_available: bool = False
    sms_available: bool = False
    auth_valid_until: float | None = None

    @property
    def effective_auth(self):
        if self.auth_valid_until is not None and monotonic() >= self.auth_valid_until:
            return min(self.auth_level, AuthLevel.RECOGNIZED)
        return self.auth_level

    def render(self):
        # Routing identifiers are intentionally excluded from model context.
        return (
            f"Current transient interface: {self.interface}; channel: {self.channel}; "
            f"transport: {self.transport}; authentication: {self.effective_auth.name}. "
            f"Screen available: {self.screen_available}; keyboard available: {self.keyboard_available}; "
            f"SMS available: {self.sms_available}. "
            "This describes this session, not your permanent identity or a lasting user fact. "
            "For voice, speak concisely; summarize code and links instead of reading them aloud. "
            "Only offer to send messages if a messaging transport is available."
        )


current_channel = ContextVar("identityos_channel", default=None)


def channel_authority(method):
    @wraps(method)
    def wrapped(self, request, *args, **kwargs):
        token = current_channel.set(request.channel_context)
        try:
            return method(self, request, *args, **kwargs)
        finally:
            current_channel.reset(token)

    return wrapped


def authorize_skill(skill_name):
    """Closed allowlist. Meta-execution and background delegation stay disabled.

    PIN does not grant an identity permissions; it only removes this additional
    channel restriction for explicitly reviewed actions. Unknown tools fail closed.
    """
    ctx = current_channel.get()
    if ctx is None:
        return True, ""
    recognized = {
        "web.search",
        "web.fetch",
        "github.read",
        "github.get_repository",
        "github.search_repositories",
        "github.review_pull_request",
        "github.find_beginner_issue",
        "github.summarize_release",
        "github.list_commits",
        "github.list_branches",
        "github.list_issues",
        "github.get_issue",
        "github.list_pull_requests",
        "calc.evaluate",
        "calc.convert",
        "calc.conversions",
        "datetime.now",
        "datetime.convert",
        "datetime.diff",
        "datetime.zones",
    }
    pin = {"email.read", "email.send"}
    required = AuthLevel.RECOGNIZED if skill_name in recognized else AuthLevel.PIN if skill_name in pin else None
    if required is None or ctx.effective_auth < required:
        return False, "Channel policy denied this capability; caller ID alone is not sensitive-action authority."
    return True, ""
