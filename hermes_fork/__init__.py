"""Fork-owned Python code for the Taznc ``next`` branch (tier T1, see FORK.md).

Upstream modules call into this package ONLY at lines marked
``# >>> FORK ANCHOR: <name> <<<``; this package may import upstream modules freely.
Tests live in ``tests/hermes_fork/``.
"""
