"""Shared environment-setting parsers."""

from __future__ import annotations

import os

TRUE_VALUES = {"true", "1", "yes"}
FALSE_VALUES = {"false", "0", "no"}


def boolean_setting(name: str, default: bool = False) -> bool:
    """Parse a true/false environment variable; reject anything ambiguous."""
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    value = raw_value.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be true or false")
