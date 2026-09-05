"""Fork-owned Python code (see docs/fork-anchor-extraction.md in the workspace repo).

Modules here hold fork-only behavior extracted out of upstream hot files.
Upstream modules call into this package ONLY at lines marked with
``# >>> FORK ANCHOR: <short-name> <<<``; this package may import upstream
modules freely. Tests live in ``tests/hermes_fork/``.
"""
