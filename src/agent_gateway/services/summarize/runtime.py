"""Single-instance lock and AI run throttle."""

import fcntl
import os
import time

from agent_gateway.core.log_utils import setup_logging as _log_setup

from agent_gateway.services.summarize.constants import (
    GATEWAY_HOME_DIR,
    LAST_RUN_FILE,
    LOCK_FILE,
    MIN_RUN_INTERVAL,
)


logger = _log_setup("summarize")


def _acquire_single_instance() -> bool:
    """Acquire the single-instance file lock. Returns False when an instance is already running (the caller exits)."""
    try:
        GATEWAY_HOME_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            os.close(fd)
            return False
    except Exception:
        return True


def _throttle_ai_run() -> bool:
    """Returns False when less than MIN_RUN_INTERVAL has elapsed since the last full summary (prevents duplicate triggering).

    Read-only check; does not update last_run -- only _mark_run_success() records the
    time after a summary completes successfully, avoiding "write at run start" making a
    retry impossible within the 6h window after a failure.
    """
    try:
        GATEWAY_HOME_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        last = (
            float(LAST_RUN_FILE.read_text().strip()) if LAST_RUN_FILE.exists() else 0.0
        )
        if now - last < MIN_RUN_INTERVAL:
            logger.info(
                f"Less than {MIN_RUN_INTERVAL // 3600} hours since the last summary, skipping (duplicate-trigger guard)"
            )
            return False
        return True
    except Exception:
        return True


def _mark_run_success() -> None:
    """Record last_run after a summary completes successfully, starting a new duplicate-prevention cooldown."""
    try:
        GATEWAY_HOME_DIR.mkdir(parents=True, exist_ok=True)
        LAST_RUN_FILE.write_text(str(time.time()))
    except Exception:
        pass
