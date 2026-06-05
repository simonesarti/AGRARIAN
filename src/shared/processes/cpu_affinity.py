import os
import logging

logger = logging.getLogger(__name__)


def pin_to_core(cpu_id: int | None) -> None:
    """Pin the calling process to a single logical CPU core.

    No-op when cpu_id is None. Logs a warning if the OS call fails
    (e.g. running without the required capability inside some container
    configurations) so the pipeline continues unaffected.
    """
    if cpu_id is None:
        return
    try:
        os.sched_setaffinity(0, {cpu_id})
        logger.info(f"[affinity] pinned to CPU {cpu_id}")
    except Exception as e:
        logger.warning(f"[affinity] could not pin to CPU {cpu_id}: {e}")
