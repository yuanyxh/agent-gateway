"""Test-time filesystem isolation.

Log paths are resolved once, at `agent_gateway.core.log_utils` import time, so
the cache root must be redirected before any test module imports the package.
`tests/__init__.py` does this for unittest discovery; this conftest covers
pytest, whose import order differs.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_CACHE = os.environ.get("AGENT_GATEWAY_TEST_CACHE") or tempfile.mkdtemp(
    prefix="agent-gateway-test-cache-"
)
os.environ["GATEWAY_CACHE"] = _CACHE
if not os.environ.get("AGENT_GATEWAY_TEST_CACHE"):
    atexit.register(shutil.rmtree, _CACHE, True)

# Some modules require QDRANT_PATH at import time; module-level setdefaults keep
# their own temporary directory when they run first.
os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
