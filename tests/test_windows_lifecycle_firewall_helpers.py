from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIREWALL_HELPER = ROOT / "tools" / "windows_lifecycle_firewall.ps1"
CLEANUP_HELPER = ROOT / "tools" / "windows_lifecycle_firewall_cleanup.ps1"


def test_firewall_helper_is_exact_outbound_block_only() -> None:
    source = FIREWALL_HELPER.read_text(encoding="utf-8")

    assert "#requires -RunAsAdministrator" in source
    assert "New-NetFirewallRule" in source
    assert "-Direction Outbound" in source
    assert "-Action Block" in source
    assert "-Program $program" in source
    assert "-RemoteAddress $nonLoopbackRemoteAddresses" in source
    assert "0.0.0.0-126.255.255.255" in source
    assert "128.0.0.0-255.255.255.255" in source
    assert "::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff" in source
    assert "Set-NetFirewallProfile" not in source
    assert "-Action Allow" not in source
    assert "-Direction Inbound" not in source
    assert "-RemoteAddress Any" not in source

    for relative_program in (
        "native\\VOOL.exe",
        "python\\python.exe",
        "python\\pythonw.exe",
        "ollama\\ollama.exe",
        "ollama\\lib\\ollama\\llama-server.exe",
    ):
        assert relative_program in source


def test_cleanup_helper_is_scoped_to_the_run_marker() -> None:
    source = CLEANUP_HELPER.read_text(encoding="utf-8")

    assert "#requires -RunAsAdministrator" in source
    assert "Get-NetFirewallRule -Group $ruleGroup" in source
    assert ".StartsWith($displayPrefix, [System.StringComparison]::Ordinal)" in source
    assert '-like "$displayPrefix*"' not in source
    assert "$_.Description -eq $ruleMarker" in source
    assert "Remove-NetFirewallRule -Name $rule.Name" in source
    assert "Set-NetFirewallProfile" not in source
