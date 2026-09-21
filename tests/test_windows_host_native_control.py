"""C# host must sign the same bytes the Python loopback service verifies."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from core.native_control import NativeControlVerifier

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ROOT / "installer" / "windows_host" / "tests" / "NativeControlChecks.csproj"


@pytest.mark.skipif(os.name != "nt", reason="Windows native host")
def test_native_host_proof_matches_api_and_rejects_replay():
    dotnet = os.environ.get("VOOL_DOTNET_EXE") or shutil.which("dotnet")
    if not dotnet:
        pytest.skip(".NET SDK is not present")
    sdk = subprocess.run([dotnet, "--list-sdks"], capture_output=True, text=True, check=True)
    if "10.0." not in sdk.stdout:
        pytest.skip(".NET 10 SDK is not present")

    stamp = int(time.time())
    run = subprocess.run(
        [dotnet, "run", "--project", str(CHECKS), "--configuration", "Release", "--", str(stamp)],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=True,
    )
    headers = json.loads(run.stdout.strip())
    verifier = NativeControlVerifier(bytes(range(32)))
    body = b'{"op":"set","mode":"auto"}'
    path = "/api/mode?source=window"
    assert verifier.verify("POST", path, body, headers)
    assert not verifier.verify("POST", path, body, headers)
    assert not NativeControlVerifier(bytes(range(32))).verify("POST", "/api/mode", body, headers)
    assert not NativeControlVerifier(bytes(range(32))).verify("POST", path, body + b" ", headers)
