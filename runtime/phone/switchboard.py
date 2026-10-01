import re
import time

from core.channels.context import AuthLevel, ChannelContext
from runtime.orchestrator import InteractionRequest


class Switchboard:
    def __init__(self, router, runtime):
        self.router, self.runtime = router, runtime

    def directory(self):
        return (
            "IdentityOS. "
            + ". ".join(f"Press {i} for {alias}" for i, alias in enumerate(self.router.identities, 1))
            + ". Press zero for directory. Press star to enter your PIN, then pound."
        )

    def process(
        self,
        caller,
        text,
        *,
        channel="sms",
        call_id="sms",
        alias=None,
        auth=AuthLevel.RECOGNIZED,
        auth_valid_until=None,
    ):
        route = self.router.resolve(caller, channel, alias, scope=call_id, auth_level=auth)
        result = self.runtime.process(
            InteractionRequest(
                identity_id=route.identity_id,
                session_id=route.session_id,
                user_id=route.user_id,
                user_input=text,
                channel_context=ChannelContext(
                    channel,
                    "sip" if channel == "voice" else "local",
                    "phone_call" if channel == "voice" else "sms",
                    caller,
                    call_id,
                    auth,
                    auth_valid_until=auth_valid_until,
                ),
            )
        )
        if not result.policy_passed:
            return "The runtime could not complete that request."
        return result.output

    def sms(self, caller, text):
        self.router.caller(caller)
        text = text.strip()
        command, _, rest = text.partition(" ")
        if command in ("/help", "/identities"):
            return "/id NAME, @NAME MESSAGE, /identities, /status, /new, /help. Available: " + ", ".join(
                self.router.identities
            )
        if command == "/id":
            if not rest:
                return "Available: " + ", ".join(self.router.identities)
            self.router.select_sms(caller, rest.strip())
            return "Active SMS identity: " + rest.strip()
        if command == "/status":
            row = self.router.caller(caller)
            return f"Active: {row['sms'] or row['preferred'] or 'unselected'}; authentication: recognized."
        if command == "/new":
            previous = self.router.resolve(caller, "sms", scope="sms")
            self.router.resolve(caller, "sms", scope="sms", new=True)
            self.runtime.end_session(previous.session_id)
            return "Started a new SMS session."
        if command.startswith("@"):
            alias = command[1:].casefold()
            self.router.identity(alias)
            return self.process(caller, rest, alias=alias) if rest else "Selected for this message: " + alias
        if command.startswith("/"):
            return "Unknown command. Use /help."
        return self.process(caller, text)


class VoiceCall:
    def __init__(self, board, caller, call_id):
        self.board, self.caller, self.call_id = board, caller, call_id
        self.alias = board.router.caller(caller)["preferred"]
        self.verified_until = 0
        self.pin = None
        self.pin_started = 0
        self.session_ids = set()

    @property
    def auth(self):
        return AuthLevel.PIN if self.verified_until > time.monotonic() else AuthLevel.RECOGNIZED

    def select(self, alias):
        self.board.router.identity(alias)
        self.alias = alias
        return "Connected to " + alias + "."

    def dtmf(self, digit):
        if self.pin is not None and time.monotonic() - self.pin_started > 30:
            self.pin = None
        if digit == "*":
            self.pin = ""
            self.pin_started = time.monotonic()
            return "Enter PIN, then pound."
        if self.pin is not None:
            if digit == "#":
                pin, self.pin = self.pin, None
                if self.board.router.verify_pin(self.caller, pin):
                    self.verified_until = time.monotonic() + 300
                    return "PIN verified for five minutes."
                self.verified_until = 0
                return "PIN verification failed."
            if digit.isdigit():
                self.pin += digit
                if len(self.pin) > 12:
                    self.pin = None
                    self.verified_until = 0
                    return "PIN entry cancelled."
            return ""
        if digit == "0":
            return self.board.directory()
        if digit.isdigit() and 1 <= int(digit) <= len(self.board.router.identities):
            return self.select(list(self.board.router.identities)[int(digit) - 1])
        return "Invalid selection. Press zero for directory."

    def text(self, text):
        if self.pin is not None and time.monotonic() - self.pin_started > 30:
            self.pin = None
        # Never transcribe speech during PIN entry into identity memory.
        if self.pin is not None:
            return "Finish PIN entry using the keypad."
        normalized = text.strip().rstrip(".?!").casefold()
        if normalized in ("switch identity", "who can i talk to", "directory"):
            return self.board.directory()
        match = re.fullmatch(r"(?:talk to|switch(?: me)? to|get me) (.+)", normalized)
        if match or normalized in self.board.router.identities:
            try:
                return self.select(match[1] if match else normalized)
            except ValueError:
                return "Unknown identity. " + self.board.directory()
        if not self.alias:
            return self.board.directory()
        alias, auth, verified_until = self.alias, self.auth, self.verified_until
        route = self.board.router.resolve(self.caller, "voice", alias, scope=self.call_id, auth_level=auth)
        self.session_ids.add(route.session_id)
        return self.board.process(
            self.caller,
            text,
            channel="voice",
            call_id=self.call_id,
            alias=alias,
            auth=auth,
            auth_valid_until=verified_until,
        )

    def close(self):
        self.pin = None
        self.verified_until = 0
        for sid in self.session_ids:
            self.board.runtime.end_session(sid)
        self.board.router.end_call(self.call_id)
