"""System subpackage — config, version, and name-normalization primitives.

Read this first if you are new to the codebase:
  - ``config.py`` defines defaults, reads ``<memory_home>/config.json``,
    and exposes ``memory_home()`` (honoring ``SILLY_MEMORY_HOME``, for test
    isolation). Almost everything else imports it.
  - ``version.py`` reads the shipped ``VERSION`` file and compares
    semver-shaped strings. Returned values fall back to
    ``UNKNOWN_VERSION`` rather than raising on missing files.
  - ``normalize.py`` parses a workspace-local name-map at
    ``<memory_home>/name-normalization.md`` (one parse per process via
    ``lru_cache``) so distilled bullets converge on canonical names.

Public interface (imported elsewhere): the ``config``, ``version``, and
    ``normalize`` submodules.
Depends on: none directly — submodules import their own dependencies.
Used by: nearly every other module.
"""
