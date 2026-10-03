from datetime import UTC, datetime
from typing import Any


class ProviderError(Exception):
    """Sanitized public provider failure; never contains credentials."""


def milliseconds(value: int | float | str) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, UTC)


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if float("-inf") < result < float("inf") else None
    except (TypeError, ValueError):
        return None
