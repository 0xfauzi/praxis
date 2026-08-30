"""Tests for the cross-process scan lock."""

from __future__ import annotations

import pytest

from praxis.storage.lock import ScanLockError, scan_lock


def test_scan_lock_blocks_a_second_holder(tmp_path):
    home = tmp_path / ".praxis"
    with scan_lock(home), pytest.raises(ScanLockError), scan_lock(home):
        pass


def test_scan_lock_releases_after_the_context(tmp_path):
    home = tmp_path / ".praxis"
    with scan_lock(home):
        pass
    # released -> re-acquire succeeds
    with scan_lock(home):
        pass
