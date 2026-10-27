"""Windows text for the catalog keys whose English names a Mac.

A key such as ``setup.thinking.local_title`` ("On my Mac") has a sibling ``setup.thinking.local_title.windows`` ("On
this computer") in every catalog. A page served on Windows asks for the sibling; every other host keeps the key it
always asked for, so a Mac is served exactly the text it was served before. The host check is
core.generated_files.host_is_windows, the one A1's Windows wording already uses.
"""
from __future__ import annotations

from core.i18n.catalog import SOURCE_LOCALE, catalog_for

WINDOWS_SUFFIX = ".windows"


def host_key(key: str, *, windows: bool | None = None) -> str:
    """``key`` + ``.windows`` on Windows when the English source carries it; ``key`` otherwise."""
    if windows is None:
        from core.generated_files import host_is_windows

        windows = host_is_windows()
    if windows:
        candidate = key + WINDOWS_SUFFIX
        if candidate in catalog_for(SOURCE_LOCALE).locale_keys:
            return candidate
    return key


__all__ = ["WINDOWS_SUFFIX", "host_key"]
