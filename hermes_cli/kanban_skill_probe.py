"""Probe entry point: report which forced skills a profile can actually load.

Run as a subprocess with ``HERMES_HOME`` pointed at the profile under test
(``python -m hermes_cli.kanban_skill_probe '["skill-a", "skill-b"]'``), it calls
the very function a worker calls at initialization —
``agent.skill_commands.build_preloaded_skills_prompt`` — and prints its verdict
as one JSON line prefixed with :data:`RESULT_PREFIX`.

A separate process is what makes the answer authoritative: skill resolution
reads config, walks external dirs and (for ``plugin:skill``) discovers and
imports that profile's plugins. Doing any of that in-process would both leak
another profile's state into ours and give an answer that is merely an
approximation of the worker's. Here the answer IS the worker's contract.
"""

from __future__ import annotations

import json
import sys

RESULT_PREFIX = "HERMES_SKILL_PROBE_RESULT:"


def main(argv: list[str]) -> int:
    names = json.loads(argv[1]) if len(argv) > 1 else []
    from agent.skill_commands import build_preloaded_skills_prompt

    # preprocess=False inside: the loader renders nothing and prompts for
    # nothing here, so probing a card is free of side effects on that profile.
    _prompt, loaded, missing = build_preloaded_skills_prompt(list(names))
    print(RESULT_PREFIX + json.dumps({"loaded": list(loaded), "missing": list(missing)}))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main(sys.argv))
