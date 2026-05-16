"""Shared shutdown-gesture machinery.

Both the physical-button watcher (``buttons.py``) and the keyboard
router (``router.py``) can arm a graceful poweroff. A single
``ShutdownGesture`` instance, shared between them, owns the
countdown task so the two input paths cooperate cleanly:

  - Buttons held → ``gesture.arm()`` (keyboard Ctrl-X during this
    is a no-op because the gesture is already armed)
  - Keyboard Ctrl-X → ``gesture.arm()`` (subsequent button hold is
    also a no-op)
  - Either input's natural cancel (button release; Esc keystroke)
    calls ``gesture.cancel()``

The countdown runs as an asyncio Task that ticks the
UIState's shutdown-progress at 20 Hz and invokes the shutdown
callback at completion. Cancellation propagates through the
standard ``asyncio.CancelledError`` path.

The hold duration matches the button gesture (5 s) so muscle
memory is consistent across input methods. ``Esc`` cancels from
the keyboard regardless of which input armed the gesture.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from minijs8.ui.state import UIState

_log = logging.getLogger(__name__)


# Default hold time — matches buttons.SHUTDOWN_HOLD_S so the gesture
# feels identical whichever input method the operator uses.
DEFAULT_SHUTDOWN_HOLD_S: float = 5.0

# 20 Hz progress-bar update rate. Tight enough to look smooth on
# the 240×240 panel without flooding the SPI bus.
_SHUTDOWN_TICK_S: float = 0.05


# Async no-arg, returns None.
ShutdownCallback = Callable[[], Awaitable[None]]


class ShutdownGesture:
    """Coordinate the SHUTTING_DOWN screen + countdown + final callback.

    Shared instance — both ButtonWatcher and InputRouter hold a
    reference. ``arm()`` is idempotent against double-arming so two
    input paths can race without producing two countdown tasks.

    Construction takes the asyncio loop reference because the
    callers may be on threaded callbacks (button GPIO events come
    from the gpiozero thread; arm() is always called from the
    asyncio thread but we keep the loop reference handy for
    diagnostics and future cross-thread arming).
    """

    def __init__(
        self,
        ui: UIState,
        loop: asyncio.AbstractEventLoop,
        shutdown_callback: ShutdownCallback,
        hold_seconds: float = DEFAULT_SHUTDOWN_HOLD_S,
    ) -> None:
        self._ui = ui
        self._loop = loop
        self._cb = shutdown_callback
        self._hold_s = float(hold_seconds)
        self._task: Optional[asyncio.Task[None]] = None

    def is_armed(self) -> bool:
        """True iff a countdown is currently running and not yet done."""
        return self._task is not None and not self._task.done()

    def arm(self, *, source: str = "unknown") -> bool:
        """Start the countdown.

        Returns True if newly armed; False if a countdown was
        already running (idempotent). ``source`` is logged for
        diagnostics ("buttons" / "keyboard Ctrl-X" / etc).
        """
        if self.is_armed():
            _log.debug("shutdown already armed; arm(%s) ignored", source)
            return False
        _log.info("shutdown armed via %s", source)
        self._ui.begin_shutdown()
        self._task = self._loop.create_task(self._countdown())
        return True

    def cancel(self, *, source: str = "unknown") -> bool:
        """Cancel a running countdown and restore the previous screen.

        Returns True if cancelled, False if nothing was running.
        Safe to call unconditionally — useful in button-release
        handlers that don't know whether the gesture was actually
        armed.
        """
        if not self.is_armed():
            return False
        _log.info("shutdown cancelled via %s", source)
        assert self._task is not None
        self._task.cancel()
        self._task = None
        self._ui.cancel_shutdown()
        return True

    async def _countdown(self) -> None:
        """Tick the progress bar at 20 Hz; call shutdown_cb on completion.

        The progress bar drains from 1.0 (full) to 0.0 (empty) over
        ``self._hold_s`` seconds. We use ``time.monotonic`` (not
        wall clock) so chrony stepping mid-countdown can't surprise
        us with a negative elapsed.

        Cancellation: ``asyncio.CancelledError`` propagates out
        cleanly. The caller (``cancel()``) has already rolled the
        UI back; we don't double-roll here.
        """
        start = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - start
                remaining_frac = max(0.0, 1.0 - elapsed / self._hold_s)
                self._ui.update_shutdown_progress(remaining_frac)
                if elapsed >= self._hold_s:
                    break
                await asyncio.sleep(_SHUTDOWN_TICK_S)
        except asyncio.CancelledError:
            # Cancel path: UIState was already rolled back by cancel().
            raise

        _log.warning("shutdown countdown complete — invoking callback")
        try:
            await self._cb()
        except Exception:
            _log.exception("shutdown callback raised")
            # If the callback failed (auth issue with polkit, transient
            # systemctl error, etc.) restore the UI so the operator
            # isn't stranded on a SHUTTING_DOWN screen forever.
            self._ui.cancel_shutdown()
