"""What the egress census counts as reaching the network (core/kas/egress_census.py).

The census counted urlopen, requests, httpx and http.client names only, so an opener
(`build_opener(...).open`, `OpenerDirector`), a raw socket (`socket.socket`,
`socket.create_connection`, `asyncio.open_connection`) or a mail/FTP client reached the network
without being counted. Each construct below is one site. Paths are reported with forward slashes on
every OS, so the ledger and the door exclusion mean the same thing on Windows.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from core.kas.egress_census import modules_with_direct_egress, scan_file, scan_tree


def _sites(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "core" / "probe.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return [site.construct for site in scan_file(path, repo_root=tmp_path)]


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.request\nurllib.request.build_opener().open('https://x')\n",
        "from urllib.request import build_opener\nopener = build_opener()\n",
        "import urllib.request\nd = urllib.request.OpenerDirector()\n",
        "from urllib.request import OpenerDirector\nd = OpenerDirector()\n",
        "import urllib.request\nurllib.request.install_opener(None)\n",
        "import socket\ns = socket.socket()\n",
        "import socket\nc = socket.create_connection(('x', 443))\n",
        "from socket import create_connection\nc = create_connection(('x', 443))\n",
        "import asyncio\nasync def f():\n    await asyncio.open_connection('x', 443)\n",
        "import smtplib\n",
        "from ftplib import FTP\n",
        "import imaplib\n",
        "import poplib\n",
        "import urllib3\n",
        "import xmlrpc.client\nxmlrpc.client.ServerProxy('https://x')\n",
    ],
)
def test_each_way_to_reach_the_network_is_one_site(tmp_path: Path, source: str) -> None:
    assert len(_sites(tmp_path, source)) == 1, source


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.request\nr = urllib.request.Request('https://x')\n",  # building a request opens nothing
        "import urllib.parse\nurllib.parse.urlsplit('https://x')\n",
        "import socket\nname = socket.gethostname()\n",
        "with open('f') as handle:\n    handle.read()\n",
    ],
)
def test_constructs_that_open_nothing_are_not_counted(tmp_path: Path, source: str) -> None:
    assert _sites(tmp_path, source) == [], source


def test_the_paths_are_forward_slashed_on_every_os() -> None:
    found = modules_with_direct_egress()
    assert found and not [path for path in found if "\\" in path]
    assert all("\\" not in site.path for site in scan_tree())


def test_the_door_is_excluded_by_its_forward_slashed_name() -> None:
    assert "core/remote_fetch_policy.py" not in modules_with_direct_egress()
