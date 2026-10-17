"""Identical source trees have one authority digest on every host filesystem."""
from __future__ import annotations

import hashlib

import pytest

import tools.windows_authority_audit as audit_tool


@pytest.mark.parametrize("native_paths", (
    ("installer/a.cs", "installer/B.cs"),
    ("installer/a/b.cs", "installer/a.cs"),
))
def test_source_order_uses_case_sensitive_relative_path_strings(tmp_path, monkeypatch, native_paths):
    contents = {
        "tools/windows_authority_audit.py": b"audit\n",
        "tools/windows_parity_inventory.py": b"inventory\n",
        **{name: f"source {index}\n".encode() for index, name in enumerate(native_paths)},
    }
    for name, data in contents.items():
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    monkeypatch.setattr(audit_tool, "ROOT", tmp_path)
    monkeypatch.setattr(audit_tool, "EFFECT_ROOTS", ())

    expected = hashlib.sha256()
    for name in sorted(contents):
        expected.update(name.encode("utf-8") + b"\0" + contents[name] + b"\0")

    assert audit_tool._source_fingerprint() == expected.hexdigest()

    # Sorting must not hide a real source edit or a filename change.
    (tmp_path / native_paths[0]).write_bytes(b"changed source\n")
    assert audit_tool._source_fingerprint() != expected.hexdigest()
    (tmp_path / native_paths[0]).write_bytes(contents[native_paths[0]])
    assert audit_tool._source_fingerprint() == expected.hexdigest()
    (tmp_path / native_paths[0]).rename(tmp_path / "installer/renamed.cs")
    assert audit_tool._source_fingerprint() != expected.hexdigest()
