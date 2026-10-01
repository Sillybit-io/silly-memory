"""silly-memory engine — local persistence and context injection.

Read this first if you are new to the codebase:
  - Importing ``memory_system`` has no side effects; import the module you
    need by its full path (``memory_system.system.config``,
    ``memory_system.events.worker``, …).
  - Implementations live in subpackages (``system``, ``storage``, ``events``,
    ``recall``, ``reflection``, ``lifecycle``, ``learning``, ``cli``,
    ``adapters``, ``eval``, ``status``, ``backends``) plus a few top-level
    modules (``paths``, ``index``, ``scope``, ``privacy``, ``safety``,
    ``preflight``, ``redact``, ``mcp_server``, ``project_mcp``).

Public interface (imported elsewhere): the subpackages and modules above.
Used by: every consumer of the ``memory_system`` package (CLIs, hooks, tests).
"""
