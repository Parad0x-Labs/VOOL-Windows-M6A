"""Every text a served page words differently off a Mac, as the page source carries it.

Only the local-models card is worded differently on every host that is not a Mac; every other text here is Windows
only. Each test file pins its own words; these tables let any of them check that a page served on another host differs
from the page served on a Mac by that host's words and nothing else.
"""
from __future__ import annotations

#: Settings > Models & Providers: the local-models card on every host but macOS.
MAC_CARD = "mkCard('local', 'This Mac', 'Local models answer without a network.', 'View local models', true)"
NEUTRAL_CARD = "mkCard('local', 'This computer', 'Local models answer without a network.', 'View local models', true)"

#: (Mac text, Windows text) for the chat page's file actions on Windows: show the folder, never open the file.
FILE_ACTION_WORDS: tuple[tuple[str, str], ...] = (
    ("Newest first; click a file to reveal it in Finder.</p>",
     "Newest first; click a file to show it in its folder.</p>"),
    ("prev.title = 'Reveal folder in Finder';",
     "prev.title = 'Show folder in File Explorer';"),
    ('title="Click to open in Preview - right-click for more">',
     'title="Opening files is not available on Windows yet - right-click for more">'),
    ("function openImageNative(local) { if (local) _fileOpen(local, false); }   // default app (Preview)",
     "function openImageNative(local) {}   // opening a file is not available on Windows yet"),
    ("if (local) add('Open in Preview', () => openImageNative(local));",
     "if (local) { const off = document.createElement('button'); off.type = 'button'; off.className = 'proj-menu-item';"
     " off.disabled = true; off.style.opacity = '0.55'; off.textContent = 'Open (not available on Windows yet)';"
     " menu.appendChild(off); }"),
    ("if (local) add('Show in Finder', () => revealImage(local));",
     "if (local) add('Show in folder', () => revealImage(local));"),
)

WINDOWS_CONSENT = "Windows shows a confirmation window; choose OK to continue."
MAC_CONSENT = "This Mac asks you to confirm it is you in its own window."
MAC_CONSENT_NOTE = "Setting, changing or resetting it asks this Mac to confirm it is you in its own window."

#: (page source on a Mac, page source on Windows, times drawn) for every place the wallet shows device approval to a
#: Windows install. The option lists keep all three methods in the same order: only the words change.
WALLET_WORDS: tuple[tuple[str, str, int], ...] = (
    ("'Export private key — unlocks with Touch ID or your Mac password only; the PIN cannot export it.'",
     "'Export private key — unlocks only with device approval on a Mac, which is not available on Windows; the PIN cannot export it.'", 1),
    ("btn.disabled = true; setMsg('Confirm with Touch ID or your Mac password…');",
     "btn.disabled = true; setMsg('Checking whether this wallet can be exported…');", 1),
    ("var METHOD_LABELS = { pin: 'PIN', password: 'password', device: 'Touch ID / Mac password' };",
     "var METHOD_LABELS = { pin: 'PIN', password: 'password', device: 'Device approval (Mac only; not available on Windows)' };", 1),
    ("[['pin', 'PIN (4–8 digits)'], ['password', 'Wallet password (10+ characters)'], ['device', 'Touch ID or my Mac password']]",
     "[['pin', 'PIN (4–8 digits)'], ['password', 'Wallet password (10+ characters)'], ['device', 'Device approval (Mac only; not available on Windows)']]", 2),
    ("'Every payment and every export will ask for Touch ID or your Mac password. Nothing is stored behind a PIN.'",
     "'Device approval is not available on Windows, so a wallet cannot be created with it here. Choose a PIN or a wallet password.'", 1),
    ("rform.appendChild(el('label', null, 'New PIN or password (none for Touch ID)'));",
     "rform.appendChild(el('label', null, 'New PIN or password'));", 1),
    ("el('div', 'vw-cx-env', 'Touch ID / Mac password approval: ' + ((policy.device && policy.device.text) || 'not available on this machine') + '.')",
     "el('div', 'vw-cx-env', 'Device approval (Mac only): ' + ((policy.device && policy.device.text) || 'not available on this machine') + '.')", 1),
    ("dev.appendChild(el('h4', null, '2. Recover with Touch ID or your Mac password'));",
     "dev.appendChild(el('h4', null, '2. Recover with device approval (Mac only; not available on Windows)'));", 1),
    ("'Device recovery was set up, but this Mac cannot release the copy right now (' + (d.reason || 'unavailable') + '). Use the backup above.'",
     "'Device recovery was set up on a Mac, but Windows cannot release that copy (' + (d.reason || 'unavailable') + '). Use the backup above.'", 1),
)
CONTACTS_WORDS: tuple[tuple[str, str, int], ...] = (
    ("err.textContent = '" + MAC_CONSENT + "';", "err.textContent = '" + WINDOWS_CONSENT + "';", 1),
    ("It never approves a payment, an email or an invitation. " + MAC_CONSENT_NOTE + "'",
     "It never approves a payment, an email or an invitation. When you set, change or reset it, " + WINDOWS_CONSENT + "'", 1),
)


#: (Mac text, Windows text) for the shortcut key caps, as the chat page's palette and companion drawer fragments carry
#: them: every shortcut already answers Ctrl, and on Windows its key cap says so.
SHORTCUT_WORDS: tuple[tuple[str, str], ...] = (
    ("p.newChat(); }, '\\⌘N');", "p.newChat(); }, 'Ctrl+N');"),
    ("p.togglePanel(); }, '\\⌘J');", "p.togglePanel(); }, 'Ctrl+J');"),
    ("p.openModelMenu(); }, '\\⌘M');", "p.openModelMenu(); }, 'Ctrl+M');"),
    ("p.openSettings(); }, '\\⌘,');", "p.openSettings(); }, 'Ctrl+,');"),
    *((f"<kbd>⌘{key}</kbd>", f"<kbd>Ctrl+{key}</kbd>") for key in "KNJ/F,"),
    ("chip.textContent = '\\u2318N';", "chip.textContent = 'Ctrl+N';"),
    ("toggleDrawer, '\\u2318/');", "toggleDrawer, 'Ctrl+/');"),
)


#: (Mac text, Windows text) for the chat filter's shortcut hint, as the chat page's sidebar fragment carries it.
FILTER_HINT_WORDS = (
    "extrasT('chats.filter_placeholder', 'Search chats\\u2026  \\u2318F')",
    "extrasT('chats.filter_placeholder.windows', 'Search chats\\u2026  Ctrl+F')",
)
#: Settings model texts that name a Mac; on Windows the page's model carries each one's ".windows" catalog text.
SETTINGS_CATALOG_KEYS = (
    "settings.group.notifications.blurb",
    "settings.row.notifications_panel.label",
    "settings.row.notifications_panel.help",
    "settings.row.calendar_accounts_panel.help",
)


def settings_catalog_words() -> tuple[tuple[str, str], ...]:
    """(Mac text, Windows text) for each Settings model text above, in every shipped language, as the page's JSON
    model carries them: each with its field name, since the page's i18n bundle carries both texts under their keys."""
    import json

    from core.i18n.catalog import CATALOGS_DIR

    pairs: list[tuple[str, str]] = []
    for path in sorted(CATALOGS_DIR.glob("*.json")):
        messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
        for key in SETTINGS_CATALOG_KEYS:
            field = key.rsplit(".", 1)[1]
            mac, windows = (f'"{field}":' + json.dumps(messages[k], ensure_ascii=False) for k in (key, key + ".windows"))
            pairs.append((mac, windows))
    return tuple(pairs)


def as_the_mac_page(page: str, host: str = "win32") -> str:
    """``page``, served on ``host`` (a sys.platform value), with that host's texts above put back to their Mac text:
    every text on Windows, only the card on any other host that is not a Mac."""
    pairs: tuple[tuple[str, str], ...] = ((MAC_CARD, NEUTRAL_CARD),)
    if host == "win32":
        pairs += (*FILE_ACTION_WORDS, *((mac, windows) for mac, windows, _drawn in WALLET_WORDS + CONTACTS_WORDS))
        pairs += (FILTER_HINT_WORDS, *settings_catalog_words())
        pairs += SHORTCUT_WORDS
    for mac_text, windows_text in pairs:
        page = page.replace(windows_text, mac_text)
    return page
