"""Self-teaching subpackage — corrections, reinforcement, contradictions, topics.

Read this first if you are new to the codebase:
  - ``ai_text_log`` captures full AI replies. ``correction_detector`` reads
    them, ``reinforcement`` applies score deltas, ``classifier`` and
    ``topic`` cluster bank lines into categories, ``contradiction`` flags
    opposing facts, and ``active_questioning`` surfaces clarifying prompts.
  - Every module here is stdlib-only and read-or-write to the per-workspace
    store — none of them reach across workspace boundaries.
  - Side effects always go through ``safety.atomic_write`` and
    ``safety.file_lock``; never call open() directly inside this subpackage.

Public interface (imported elsewhere): the submodules listed above.
Depends on: none directly — submodules import their own dependencies.
Used by: cli.cli_learn_status, cli.cli_inspect (topic),
    events.observer (ai_text_log), reflection.reflector_v2.
"""
