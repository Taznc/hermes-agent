"""Kanban dashboard plugin backend package — see plugin_api.py for the facade,
mount point, and nearly all routes; dispatch_pause_router.py is the one
genuinely fork-only slice extracted into a self-contained sibling module
(zero upstream commits touch it). See t_2e2c6479 for why the other candidate
extractions (boards/recovery/worker-visibility/attachments/a shared _common)
were reverted: they relocated upstream-owned code and increased merge-conflict
surface against upstream/main instead of reducing it.
"""
