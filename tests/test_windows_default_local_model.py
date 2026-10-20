"""The Windows edition's default local model is qwen3:8b, the model VOOL certifies as an author.

qwen2.5:7b fails the local-model certification (result_continuation, missing_tool_calls; see
docs/APPLE_CAPABILITY_MATRIX.md), so a Windows install that defaulted to it launched but refused
to write any answer. The bundle manifest written by build_bundle.ps1 is the model source for the
installed app (core/bundle_manifest.py), and the script installer and launchers fall back to a
fixed tag when nothing else names one. Every one of those Windows defaults must be the certified
author, and must agree with the macOS/Linux installer's own fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CERTIFIED_AUTHOR = "qwen3:8b"


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_bundle_build_defaults_the_manifest_model_to_the_certified_author() -> None:
    script = _read("installer/bundle/build_bundle.ps1")
    defaults = re.findall(r'\[string\]\$Model\s*=\s*"([^"]+)"', script)
    assert defaults == [CERTIFIED_AUTHOR]
    # The manifest's selected_model is the parameter, so the default reaches the installed app.
    assert re.search(r"^\s*selected_model\s*=\s*\$Model\s*$", script, re.MULTILINE)


def test_windows_script_installer_falls_back_to_the_certified_author() -> None:
    script = _read("installer/install_vool.bat")
    fallbacks = re.findall(r'if "%MODEL_TAG%"=="" set "MODEL_TAG=([^"]+)"', script)
    assert fallbacks == [CERTIFIED_AUTHOR]


def test_windows_launchers_fall_back_to_the_certified_author() -> None:
    for launcher in ("Start_VOOL.bat", "Talk_To_VOOL.bat"):
        script = _read(launcher)
        fallbacks = re.findall(r'if "%VOOL_OLLAMA_MODEL%"=="" set "VOOL_OLLAMA_MODEL=([^"!%]+)"', script)
        assert fallbacks == [CERTIFIED_AUTHOR], launcher


def test_windows_fallback_matches_the_posix_installer_fallback() -> None:
    script = _read("installer/install_vool.sh")
    assert f'") 2>/dev/null || echo "{CERTIFIED_AUTHOR}"' in script
    assert f"PRIMARY_LOCAL_MODEL={CERTIFIED_AUTHOR}" in script


def test_no_windows_default_names_the_uncertified_model() -> None:
    for relative in (
        "installer/bundle/build_bundle.ps1",
        "installer/install_vool.bat",
        "Start_VOOL.bat",
        "Talk_To_VOOL.bat",
    ):
        script = _read(relative)
        assert not re.search(r'(?:\$Model\s*=|MODEL_TAG=|VOOL_OLLAMA_MODEL=)\s*"?qwen2\.5:7b', script), relative
