"""VOOL core package."""

# Windows 0.7.1: VOOL runs no git. Every VOOL runtime process (an app, a bundle host or a core
# module as its entry, or a child of an armed process) refuses git before any process starts, from
# the moment core is first imported (core/safe_git.py). Linux and macOS: nothing is installed.
from core.safe_git import arm_spawn_guard_for_this_process as _arm_git_spawn_guard

_arm_git_spawn_guard()
del _arm_git_spawn_guard
