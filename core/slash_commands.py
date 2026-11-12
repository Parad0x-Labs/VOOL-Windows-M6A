"""The slash commands the Windows VOOL chat really answers, one table for every place that lists them.

`/help` renders its reply from this table (core/agent_runtime/fast_paths_utility.py), and the "/"
composer menu is built from the same rows, so the two can never disagree. Every row is a command the
Windows chat answers when typed:

- `/help` and `/new` are answered by the slash-command lane (`ui_command_fast_path`); `/new` points
  to the "+ New" button at the top left of the sidebar, which is where a new chat starts.
- `/stop` is taken by the chat page before it is sent: it presses the running task's Stop control,
  the one Esc presses (`send()` in core/vool_chat_page.py).
- `/stopall` is core/vool_agent_brake.py, with its two-step `/stopall confirm` (60 seconds,
  `_STOPALL_CONFIRM_SECONDS`).
- `/trace` gives the link to the trace rail the API serves at /trace.

Descriptions are plain text, one line each, with no Markdown, so any surface can show them as they are.
"""

from __future__ import annotations

WORKING_SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "List the commands that work in this chat."),
    ("/new", "Show where to start a new chat: the + New button at the top left."),
    ("/stop", "Stop the reply that is running, the same as pressing Esc."),
    (
        "/stopall",
        "Stop all of VOOL (the window, the API and the local model runner). "
        "It asks first: type /stopall confirm within 60 seconds to go ahead.",
    ),
    ("/trace", "Get the link to the live trace rail."),
)

__all__ = ["WORKING_SLASH_COMMANDS"]
