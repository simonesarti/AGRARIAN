"""One place to decide how loud the pipeline's workers are.

Every worker module builds its own logger and file handler at import time, and
each used to end that block with a hardcoded `setLevel(logging.WARNING)`. That is
a reasonable default — an INFO line per frame at 30 fps fills a disk — but as a
*constant* it means the diagnostics the workers already write can never be turned
on. Debugging a flight that produced no alerts, or a telemetry plane that
connected and delivered nothing, then comes down to reading code and guessing,
because the lines that would have said so were dropped before reaching a handler.

The default is unchanged, so nothing gets louder by accident. Setting LOG_LEVEL
in the app container's environment is what changes it, and the orchestrator
forwards it like any other APP_ENV_* setting.

Read once at import rather than per call: these modules set their level while the
module object is being created, and a value that changed mid-flight would apply
to some workers and not others.
"""

import logging
import os

_DEFAULT_LEVEL = logging.WARNING


def _resolve() -> int:
    raw = os.environ.get("LOG_LEVEL", "").strip().upper()
    if not raw:
        return _DEFAULT_LEVEL
    level = logging.getLevelName(raw)
    # getLevelName returns the string "Level <name>" for anything it does not
    # know, so an int is the only proof the name was real. Falling back rather
    # than raising: a typo here should not stop a flight from taking off.
    if not isinstance(level, int):
        logging.getLogger("main").warning(
            f"LOG_LEVEL='{raw}' is not a known level name. "
            f"Falling back to {logging.getLevelName(_DEFAULT_LEVEL)}."
        )
        return _DEFAULT_LEVEL
    return level


WORKER_LOG_LEVEL = _resolve()


def worker_log_level() -> int:
    """The level every pipeline worker's logger should be set to."""
    return WORKER_LOG_LEVEL
