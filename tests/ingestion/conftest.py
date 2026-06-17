"""Ingestion test suite conftest.

Defense-in-depth fixture: ensures a current event loop is installed before
any ingestion test runs, so that synchronous test code that calls
``asyncio.run()`` works correctly even if a prior test suite left
``asyncio.set_event_loop(None)`` as its teardown state.

Root cause of the original pollution (documented here for future maintainers):
  pytest-asyncio >= 0.22 / 1.x uses ``_temporary_event_loop`` to install and
  then restore the previous loop around each async-test runner.  When the
  session-scoped runner is first created there is *no* current loop, so
  ``old_loop = None``.  After the last async test in tests/control_plane
  completes, pytest-asyncio calls ``asyncio.set_event_loop(None)``
  (restoring the None it captured at startup).  Any subsequent synchronous
  test that calls the deprecated ``asyncio.get_event_loop()`` then raises
  ``RuntimeError: There is no current event loop in thread 'MainThread'``.

The PRIMARY fix is that all ingestion test methods now use ``asyncio.run()``
(self-contained; creates its own loop) instead of
``asyncio.get_event_loop().run_until_complete()``.  This fixture is a
secondary safeguard that is dormant when the loop is already healthy.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True, scope="session")
def _ensure_event_loop_for_ingestion_sync_tests():
    """Guarantee a non-closed event loop is installed for the ingestion session.

    ``asyncio.run()`` creates its own loop and does not depend on the global
    event loop, so this fixture is purely defensive.  It is a no-op when the
    global loop is already healthy.  It always yields (required for session
    fixtures) so pytest-asyncio does not raise 'did not yield a value'.
    """
    loop_installed = False
    loop: asyncio.AbstractEventLoop | None = None

    try:
        existing = asyncio.get_event_loop_policy().get_event_loop()
        if existing is None or existing.is_closed():
            raise RuntimeError("loop absent or closed")
        # Already healthy — no action needed.
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_installed = True

    yield  # always yield so pytest-asyncio sees a proper generator fixture

    if loop_installed and loop is not None:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass
        asyncio.set_event_loop(None)
