"""Session-wide isolation for the production-data guard (ruling 2026-10-09).

The test process is never a production unit, so ``default_data_dir()`` hands it
the scratch directory and ``refresh_snapshot()`` would copy production data
into it. Point both at throwaway paths for the whole session: no test may read
from, snapshot, or write to the real data directory of the host it runs on."""
import os
import tempfile

import pytest

_SESSION_ROOT = tempfile.mkdtemp(prefix="agentic-tests-")
os.environ.setdefault("AGENTIC_SCRATCH_DATA_DIR", os.path.join(_SESSION_ROOT, "scratch-data"))
os.environ.setdefault("AGENTIC_PRODUCTION_DATA_DIR", os.path.join(_SESSION_ROOT, "no-production-here"))


@pytest.fixture(scope="session")
def session_root() -> str:
    return _SESSION_ROOT
