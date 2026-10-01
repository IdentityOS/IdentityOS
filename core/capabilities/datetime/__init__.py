from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult

_KNOWN_ZONES = {
    "UTC": 0,
    "GMT": 0,
    "EST": -5,
    "CST": -6,
    "MST": -7,
    "PST": -8,
    "CET": 1,
    "EET": 2,
    "IST": 5.5,
    "JST": 9,
    "AEST": 10,
    "NZST": 12,
}


@register
class DateTimeCapability(Capability):
    inspection_readiness = "ready"
    id = "datetime"
    name = "DateTime"
    version = "1.1.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Get current time in any timezone, convert between zones, calculate date differences"
    permissions = ["public"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.datetime", {"installed_at": None})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.datetime")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## DateTime Skills (MANDATORY — use when asked about time/date)",
            "When the user asks for the current time, date, or timezone conversion, you MUST use the skills below.",
            "Do NOT say you don't have real-time access. You DO. Use the skills.",
        ]

    _SKILLS = [
        Skill(
            name="datetime.now",
            description="Get current date and time in a timezone",
            permission="public",
            input_schema=object_schema({"tz_name": {"type": "string"}}),
            verification_params={"tz_name": "UTC"},
        ),
        Skill(
            name="datetime.convert",
            description="Convert a time between timezones",
            permission="public",
            input_schema=object_schema(
                {
                    "dt_str": {"type": "string"},
                    "from_tz": {"type": "string"},
                    "to_tz": {"type": "string"},
                },
                required=("dt_str", "from_tz", "to_tz"),
            ),
        ),
        Skill(
            name="datetime.diff",
            description="Calculate days between two dates",
            permission="public",
            input_schema=object_schema(
                {"date1": {"type": "string"}, "date2": {"type": "string"}},
                required=("date1", "date2"),
            ),
        ),
        Skill(
            name="datetime.zones",
            description="Describe supported timezone identifiers",
            permission="public",
            input_schema=object_schema(),
            verification_params={},
        ),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        import time as _time
        _t0 = _time.monotonic()
        try:
            dispatch = {
                "datetime.now": self._now,
                "datetime.convert": self._convert,
                "datetime.diff": self._diff,
                "datetime.zones": self._zones,
            }
            handler = dispatch.get(skill_name)
            if handler is None:
                return CapabilityResult.fail("datetime", skill_name, "unknown_skill", f"Unknown skill: {skill_name}")
            data = handler(**params)
            return CapabilityResult.from_data(
                "datetime",
                skill_name,
                data,
                source="system clock",
                duration_ms=(_time.monotonic() - _t0) * 1000,
            )
        except Exception as e:
            return CapabilityResult.fail(
                "datetime",
                skill_name,
                type(e).__name__,
                str(e),
                duration_ms=(_time.monotonic() - _t0) * 1000,
            )

    @staticmethod
    def _timezone(tz_name: str) -> tuple[tzinfo, str]:
        cleaned = tz_name.strip()
        upper = cleaned.upper()
        if upper in _KNOWN_ZONES:
            return timezone(timedelta(hours=_KNOWN_ZONES[upper])), upper
        try:
            return ZoneInfo(cleaned), cleaned
        except (ZoneInfoNotFoundError, ValueError):
            abbreviations = ", ".join(_KNOWN_ZONES)
            raise ValueError(
                f"Unknown timezone: {tz_name}. Use an IANA timezone such as "
                f"America/Chicago or one of: {abbreviations}"
            ) from None

    @staticmethod
    def _offset_hours(value: datetime) -> float:
        offset = value.utcoffset()
        return offset.total_seconds() / 3600 if offset is not None else 0.0

    @staticmethod
    def _localize(value: datetime, zone: tzinfo, label: str) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(zone)

        first = value.replace(tzinfo=zone, fold=0)
        second = value.replace(tzinfo=zone, fold=1)
        if first.utcoffset() == second.utcoffset():
            return first

        def round_trips(candidate: datetime) -> bool:
            return (
                candidate.astimezone(timezone.utc)
                .astimezone(zone)
                .replace(tzinfo=None)
                == value
            )

        first_valid = round_trips(first)
        second_valid = round_trips(second)
        if first_valid and second_valid:
            raise ValueError(
                f"Ambiguous local time {value.isoformat(sep=' ')} in {label}. "
                "Include an explicit UTC offset in dt_str."
            )
        if not first_valid and not second_valid:
            raise ValueError(
                f"Nonexistent local time {value.isoformat(sep=' ')} in {label} "
                "due to a timezone transition."
            )
        return first if first_valid else second

    def _now(self, tz_name: str = "UTC", **kwargs: Any) -> dict[str, Any]:
        tz, label = self._timezone(tz_name)
        now = datetime.now(tz)
        return {
            "timezone": label,
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "utc_offset_hours": self._offset_hours(now),
            "weekday": now.strftime("%A"),
        }

    def _convert(
        self,
        dt_str: str = "",
        from_tz: str = "UTC",
        to_tz: str = "UTC",
        **kwargs: Any,
    ) -> dict[str, Any]:
        source_tz, source_label = self._timezone(from_tz)
        target_tz, target_label = self._timezone(to_tz)
        dt = datetime.fromisoformat(dt_str) if dt_str else datetime.now()
        source = self._localize(dt, source_tz, source_label)
        converted = source.astimezone(target_tz)
        delta = self._offset_hours(converted) - self._offset_hours(source)
        return {
            "input": {"datetime": dt_str, "timezone": source_label},
            "output": {
                "datetime": converted.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": target_label,
            },
            "difference_hours": delta,
        }

    @staticmethod
    def _diff(date1: str = "", date2: str = "", **kwargs: Any) -> dict[str, Any]:
        d1 = datetime.strptime(date1, "%Y-%m-%d") if date1 else datetime.now()
        d2 = datetime.strptime(date2, "%Y-%m-%d") if date2 else datetime.now()
        diff = abs((d2 - d1).days)
        return {
            "date1": d1.strftime("%Y-%m-%d"),
            "date2": d2.strftime("%Y-%m-%d"),
            "days_between": diff,
            "weeks_between": round(diff / 7, 1),
        }

    @staticmethod
    def _zones(**kwargs: Any) -> dict[str, Any]:
        return {
            "timezones": _KNOWN_ZONES,
            "iana_timezones_supported": True,
            "iana_format": "Area/Location",
            "iana_examples": ["America/Chicago", "Europe/London", "Asia/Tokyo"],
        }
