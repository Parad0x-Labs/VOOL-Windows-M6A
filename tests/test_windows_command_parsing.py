"""On Windows a command's backslashes are path separators, never escapes (sandbox/network_guard.py).

The sandbox split every command the POSIX way, so on Windows `C:\\Users\\me\\venv\\Scripts\\python.exe`
became `C:Usersme...python.exe`: the whitelist saw a base command named `c:usersme...python` and
refused it (blocked_by_policy), and the network guard could not recognise `C:\\...\\curl.exe` as curl.
"""
from __future__ import annotations

import pytest

from sandbox import network_guard
from sandbox.network_guard import command_uses_network, parse_command


@pytest.fixture()
def windows(monkeypatch):
    monkeypatch.setattr(network_guard, "_WINDOWS", True)


@pytest.fixture()
def posix(monkeypatch):
    monkeypatch.setattr(network_guard, "_WINDOWS", False)


def test_on_windows_a_path_keeps_its_backslashes(windows):
    argv = parse_command(r"C:\Users\me\venv\Scripts\python.exe -m pytest -q tests\test_a.py")
    assert argv == [r"C:\Users\me\venv\Scripts\python.exe", "-m", "pytest", "-q", r"tests\test_a.py"]


def test_on_windows_quotes_still_group_a_path_with_spaces(windows):
    argv = parse_command(r'"C:\Program Files\Python312\python.exe" -c "print(1)"')
    assert argv == [r"C:\Program Files\Python312\python.exe", "-c", "print(1)"]


def test_on_windows_a_full_path_to_curl_is_still_curl(windows):
    assert command_uses_network(parse_command(r"C:\Windows\System32\curl.exe https://example.com"))
    assert command_uses_network(parse_command(r"C:\Windows\System32\CURL.EXE example.com"))


def test_off_windows_a_backslash_still_escapes(posix):
    assert parse_command(r"echo a\ b") == ["echo", "a b"]
    # Off Windows `.exe` is part of the name: the Windows branch alone reads `curl.exe` as curl.
    assert not command_uses_network(["curl.exe", "example.com"])


def test_on_windows_a_hash_is_an_ordinary_character_as_in_the_posix_split(windows):
    assert parse_command(r"python -c print(1)#x tests\a#b.py") == ["python", "-c", "print(1)#x", r"tests\a#b.py"]


@pytest.mark.parametrize(
    ("command", "argv"),
    (
        (r'python -c "print(\"hi\")"', ["python", "-c", 'print("hi")']),
        (r'echo "a\"b"', ["echo", 'a"b']),
        (r'echo a\\"b c"', ["echo", "a\\b c"]),
        (r'echo a\\\"b', ["echo", 'a\\"b']),
        (r'echo "C:\dir\\"', ["echo", "C:\\dir\\"]),
    ),
)
def test_on_windows_backslashes_before_a_quote_follow_the_windows_argv_rules(windows, command, argv):
    # 2n backslashes before a quote give n and the quote groups; 2n+1 give n and a literal quote.
    assert parse_command(command) == argv


def test_on_windows_a_single_quoted_path_from_shlex_join_stays_one_argument(windows):
    argv = parse_command(r"'C:\VOOL Windows\.venv\Scripts\python.exe' -m pytest")
    assert argv == [r"C:\VOOL Windows\.venv\Scripts\python.exe", "-m", "pytest"]


@pytest.mark.parametrize(
    "command",
    (
        r"C:\Windows\System32\curl.exe. example.com",
        '"curl.exe " example.com',
        "curl. example.com",
        "CURL.COM example.com",
        "npm.cmd install left-pad",
    ),
)
def test_on_windows_a_program_name_is_read_as_windows_runs_it(windows, command):
    # Windows drops trailing dots and spaces from a name and finds a program by its PATHEXT extension.
    assert command_uses_network(parse_command(command))
