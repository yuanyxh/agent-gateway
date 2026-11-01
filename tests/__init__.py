"""Redirect test logging away from the developer's live cache.

`setup_logging()` resolves its file at import time. Without this, a test run
appends to the live `~/.cache/utility/server.log`, which makes production logs
and test output indistinguishable.

Only the cache root is redirected: `GATEWAY_HOME` stays free because several
tests point `UTILITY_DATA_DIR` at their own temp root and rely on that being
what the config layer resolves.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_ROOT = os.environ.get("AGENT_GATEWAY_TEST_CACHE") or tempfile.mkdtemp(
    prefix="agent-gateway-tests-"
)
os.environ["GATEWAY_CACHE"] = _ROOT
atexit.register(shutil.rmtree, _ROOT, True)
