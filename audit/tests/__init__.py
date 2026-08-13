"""Tests for the audit harness.

Deliberately not under ``tests/``. That directory is in the sdist's ``include``
list and in ``testpaths``, so a test living there and importing ``audit`` would
fail at collection on an unpacked sdist -- where ``tests/`` is present and
``audit/`` is not. CI runs this directory as a second pytest invocation instead.
"""
