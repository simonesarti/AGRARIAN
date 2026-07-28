"""Signal disposition for pipeline child processes.

The orchestrator installs SIGTERM/SIGINT handlers so that a container stop, a pod
eviction, or an operator Ctrl+C becomes a coordinated shutdown via error_event.
Because the pipeline uses the fork start method, every child inherits those
handlers at fork time, which breaks teardown in two ways:

- SIGTERM: the orchestrator's cleanup path escalates to Process.terminate(), which
  sends SIGTERM to the child. With the inherited handler the child merely records
  the signal and keeps running, so the join never completes and teardown hangs.
- SIGINT: Ctrl+C is delivered to every process in the foreground process group, so
  children would race the parent to react to the same signal.

Children therefore restore SIGTERM to the default disposition (terminate at once,
so Process.terminate() works as the parent's force-kill escalation) and ignore
SIGINT outright, leaving all shutdown sequencing to the parent. Note that SIG_IGN
rather than SIG_DFL is deliberate for SIGINT: the default disposition would kill
the child at the C level, skipping the `finally` blocks that detach from shared
memory and set work_finished.

Resetting in the child rather than deferring the parent's signal.signal() calls
until after the fork is what lets the orchestrator arm its handlers immediately,
so a signal arriving during the multi-second model-loading startup is still
handled cleanly.
"""

import signal


def reset_child_signal_handlers() -> None:
    """Drop the signal handlers inherited from the orchestrator.

    Must be the first statement of a pipeline process's run(), before any work
    that could block: until it executes, the child still carries the parent's
    handlers.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
