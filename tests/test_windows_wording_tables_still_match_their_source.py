"""Every Windows word swap still finds the text it replaces.

The wallet and Contacts fragments swap Mac wording for Windows wording with plain ``str.replace`` on the page
source, so a swap whose source text changed upstream silently does nothing and Windows is served the Mac words
(the 0.7.2 merge: Mac reworded device approval to "your computer's own sign-in (such as Touch ID)" and the
consent line to "Your computer now asks you...", and the Windows page said "Touch ID"). Each swap's source text
must appear in the source it is applied to.
"""
from __future__ import annotations

import pytest

from core import contacts_fragment, wallet_fragment


@pytest.mark.parametrize(
    ("table", "source"),
    [
        (wallet_fragment._WINDOWS_WORDING, wallet_fragment._WALLET_JS),
        (contacts_fragment._WINDOWS_WORDING, contacts_fragment._JS),
    ],
    ids=["wallet", "contacts"],
)
def test_every_windows_swap_finds_its_mac_text(table, source) -> None:
    missing = [mac for mac, _windows in table if mac not in source]
    assert missing == [], f"these Windows swaps no longer match their source and would do nothing: {missing}"
