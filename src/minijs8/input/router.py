"""Input router.

Receives ``KeyEvent`` from any input source (keyboard thread, eventually
also synthesized events from tests) and applies the right effect:

  - **Global hotkeys** (Ctrl-S, Ctrl-Q, Ctrl-H) work from any screen,
    any mode, except when an edit is in progress (so Ctrl-S in the
    middle of typing a callsign doesn't jump screens — it'd be lost
    typing).
  - **Edit mode**: when a field is being edited, all printable keys go
    into the edit buffer, Backspace deletes, Enter commits, Esc reverts.
  - **Ring navigation**: ←/→ cycle the screen ring; locked when the
    station is unconfigured (per Step 3 spec — operator must finish
    Setup or use Emergency Beacon bypass).
  - **Field focus**: Tab/Shift-Tab cycle the focused field within a
    screen.
  - **Activation**: Enter on a focused interactive element fires the
    appropriate action (start edit, jump screen, arm beacon, …).

The router is purely a function of state; it has no I/O. That makes
the entire keyboard pipeline testable without GPIO, evdev, or the TFT.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from minijs8.input.events import Key, KeyEvent
from minijs8.ui.state import ComposeCmd, Screen, UIState

_log = logging.getLogger(__name__)


# Maximum input lengths for editable fields. Beyond these we ignore
# additional characters — better than truncating after-the-fact.
_MAX_CALLSIGN = 10
_MAX_GRID = 6
_MAX_UNITS = 5  # "miles" / "km"
# Groups field: comma-separated list of up to 4 entries, each '@' +
# 1-8 alphanumeric/slash chars. Worst case ~44 chars; allow 64 for
# operator whitespace before the config validator normalises.
_MAX_GROUPS_FIELD = 64
# Max length for the freq_hz edit buffer. Up to "14078000" (8 chars) for
# Hz form, or "14.078" (6 chars) for MHz form. 12 is comfortable margin.
_MAX_FREQ_HZ = 12


# Type alias for the emergency-bypass action (jumps to Emergency screen
# with N0CALL identity, gated on GPS fix in Step 4).
EmergencyBypass = Callable[[], None]


class InputRouter:
    """Translate KeyEvents into UIState mutations.

    Construct with the UIState, a callback for atomic config saves,
    and a callback for emergency bypass. Feed it KeyEvents via
    ``handle()``.
    """

    def __init__(
        self,
        ui: UIState,
        save_config: Callable[..., bool],
        emergency_bypass: EmergencyBypass,
        set_frequency: Optional[Callable[[int], bool]] = None,
        cycle_radio: Optional[Callable[[], bool]] = None,
        mark_inbox_read: Optional[Callable[[int], bool]] = None,
        delete_inbox_row: Optional[Callable[[int], bool]] = None,
        compose_send: Optional[
            Callable[[str, "ComposeCmd", str, str], bool]
        ] = None,
        compose_store: Optional[Callable[[str, str], bool]] = None,
        allcall_query_msgs: Optional[Callable[[], bool]] = None,
        allcall_cq: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._ui = ui
        # save_config(callsign, grid, units, new_groups=None) -> True
        # if saved cleanly. ``new_groups`` is an optional kwarg added
        # for the May 2026 groups feature — apps that pass the older
        # 3-arg signature still work via the kwarg's default.
        # The UIState refreshes its identity from a successful save.
        self._save_config = save_config
        self._emergency_bypass = emergency_bypass
        # set_frequency(hz) -> True if the radio accepted the change.
        # Optional because the daemon may run without a CAT-capable
        # radio attached (headless tests, broken hardware). When None,
        # the freq_hz field on Setup behaves as read-only (commit fails).
        self._set_frequency = set_frequency
        # cycle_radio() -> True if the new radio_id was saved cleanly.
        # The callback is responsible for picking the next id from the
        # registry, updating UIState, and writing config.toml. The
        # daemon must be restarted for the change to take effect; the
        # Setup screen surfaces a "(restart)" hint in that interval.
        # Optional for testability — None disables the cycle.
        self._cycle_radio = cycle_radio
        # mark_inbox_read(row_id) -> True if the mailbox UPDATE took
        # effect (row was UNREAD; now it's READ). Called when the
        # operator opens detail-view on an inbox row. Optional so
        # tests can construct a router without a mailbox.
        self._mark_inbox_read = mark_inbox_read
        # delete_inbox_row(row_id) -> True if the row was DELETED from
        # the persistent store. Called when the operator presses
        # Delete on an inbox row. Hard delete — the row is gone from
        # inbox.db permanently. Optional so tests can construct a
        # router without a mailbox; when None, the in-memory cache
        # still updates so the UI stays responsive (but the row will
        # come back on the next refresh from disk).
        self._delete_inbox_row = delete_inbox_row
        # compose_send(to, cmd, text) -> True if the compose was
        # accepted by the outbound queue. The callback handles wire-
        # format building (using the station's grid for MYLOC) and
        # enqueue. Optional so tests can construct a router without
        # an outbound queue; when None, the SEND button is a visual
        # no-op (compose state still clears so the UI doesn't get
        # stuck).
        self._compose_send = compose_send
        # compose_store(to, text) → True if the local mailbox row
        # was written. Called for CMD=STORE, where nothing transmits
        # but a row is added to inbox.db keyed for the TO callsign.
        # When None (tests without a mailbox), STORE SEND silently
        # no-ops and keeps the operator on COMPOSE.
        self._compose_store = compose_store
        # allcall_query_msgs() -> True if the @ALLCALL QUERY MSGS
        # broadcast was enqueued. Called when the operator presses
        # Enter on the QUERY MSGS row of the ALLCALL screen. Optional
        # so tests can construct a router without an outbound queue;
        # when None, the row is a visual no-op.
        self._allcall_query_msgs = allcall_query_msgs
        # allcall_cq() -> True if the CQ broadcast was enqueued.
        # Called when the operator presses Enter on the CQ row of the
        # ALLCALL screen. Optional, same fallback as above.
        self._allcall_cq = allcall_cq

    def handle(self, event: KeyEvent) -> None:
        """Top-level dispatcher. Wraps any handler exception so a
        single bad key doesn't take the input subsystem down."""
        try:
            self._handle(event)
        except Exception:
            _log.exception("input router raised on event=%r", event)

    def _handle(self, event: KeyEvent) -> None:
        snapshot = self._ui.snapshot()

        # 1) If we're in edit mode, the field eats every key first.
        if self._ui.is_editing():
            self._handle_edit_key(event)
            return

        # 2) Global hotkeys (only when NOT editing).
        if event.key is not None and self._handle_global_hotkey(event.key, snapshot):
            return

        # 3) Per-screen handling.
        self._handle_screen_key(event, snapshot)

    # ── Edit mode ────────────────────────────────────────────────────

    def _handle_edit_key(self, event: KeyEvent) -> None:
        """Field is being edited. Every key goes here."""
        if event.key is Key.ENTER:
            self._commit_edit()
            return
        if event.key is Key.ESC or event.key is Key.CTRL_C:
            self._ui.cancel_edit()
            return
        if event.key is Key.BACKSPACE:
            self._ui.edit_backspace()
            return
        if event.char is not None:
            # Drop characters that exceed the field's max length.
            field = self._ui.editing_field()
            buf = self._ui.edit_buffer()
            limit = self._field_max_len(field)
            if len(buf) >= limit:
                return
            # JS8Call's wire protocol is uppercase-only — callsigns,
            # grids, command verbs, free-text bodies all transmit
            # uppercase. We normalise at input time so the operator's
            # keyboard state (Shift / CapsLock / autocorrect) doesn't
            # produce mixed-case content that would either fail to
            # transmit cleanly or render inconsistently against
            # peers' uppercase displays. The single-character
            # ``.upper()`` is a no-op for digits, punctuation, and
            # already-uppercase letters — only ``a..z`` flip.
            #
            # Exception: ``units`` is a UI-local preference (miles vs
            # km) that never appears on the air, and the existing
            # validator rejects ``KM`` / ``MILES`` so uppercasing
            # would break the operator's input. Skip the uppercase
            # conversion for that field only.
            ch = event.char if field == "units" else event.char.upper()
            self._ui.edit_append(ch)

    def _field_max_len(self, field_name: Optional[str]) -> int:
        if field_name == "callsign":
            return _MAX_CALLSIGN
        if field_name == "grid":
            return _MAX_GRID
        if field_name == "units":
            return _MAX_UNITS
        if field_name == "freq_hz":
            return _MAX_FREQ_HZ
        if field_name == "groups":
            # Worst-case: 4 groups × (1 "@" + 8 chars + ", ") = ~44.
            # Use 64 for some breathing room and any operator who
            # types extra spaces before commit (we strip on save).
            return _MAX_GROUPS_FIELD
        return 0

    def _commit_edit(self) -> None:
        """Validate and write the in-progress edit; refresh identity."""
        field = self._ui.editing_field()
        buf = self._ui.edit_buffer()
        if field is None:
            return

        # Frequency edits don't touch the persistent config — they
        # change the radio's VFO directly via CAT. Validation happens
        # in the set_frequency callback (or here for the parse).
        if field == "freq_hz":
            self._commit_frequency_edit(buf)
            return

        # Read the OTHER current values from the snapshot so we save a
        # complete config, not a partial one.
        snap = self._ui.snapshot()
        new_call = snap.callsign
        new_grid = snap.grid
        new_units = snap.units
        new_groups = snap.groups

        if field == "callsign":
            new_call = buf
        elif field == "grid":
            new_grid = buf
        elif field == "units":
            new_units = buf
        elif field == "groups":
            # Hand the raw operator input straight to the save path —
            # config._validate_groups handles comma-splitting,
            # uppercase normalisation, dedup, implicit-group filtering,
            # and per-entry format/length validation. On any failure
            # the save callback returns False and we amber-warn so
            # the operator can correct without losing typed content.
            new_groups = buf

        ok = self._save_config(new_call, new_grid, new_units, new_groups=new_groups)
        if ok:
            self._ui.commit_edit()
        else:
            # Show the operator that the value was rejected. The save
            # function is expected to log the reason; we surface it by
            # marking the edit as invalid (UIState handles this).
            self._ui.mark_edit_invalid()

    def _commit_frequency_edit(self, buf: str) -> None:
        """Parse a frequency edit (in MHz) and push to CAT.

        Accepts forms like "7.078", "7078", "7,078" (comma replaced
        with dot for European keyboards). Out-of-range values are
        rejected by the CAT layer or the radio itself.
        """
        text = buf.strip().replace(",", ".")
        if not text:
            self._ui.mark_edit_invalid()
            return

        # Heuristic: if the value is < 1000, treat as MHz; otherwise
        # treat as Hz directly. Operators may type "7.078" or "7078000".
        try:
            if "." in text:
                hz = int(round(float(text) * 1_000_000))
            else:
                value = int(text)
                hz = value if value >= 1_000_000 else value * 1_000_000
        except ValueError:
            self._ui.mark_edit_invalid()
            return

        # Sanity bounds: HF amateur range plus a generous margin. The
        # QDX is band-limited by hardware; out-of-range values get
        # rejected by the radio anyway, but flagging them at the UI
        # layer gives faster feedback.
        if not (100_000 <= hz <= 60_000_000):
            self._ui.mark_edit_invalid()
            return

        if self._set_frequency is None:
            # No CAT — can't change the radio's actual frequency.
            self._ui.mark_edit_invalid()
            return

        if self._set_frequency(hz):
            # Success: update the displayed frequency and exit edit mode.
            self._ui.set_freq_hz(hz)
            self._ui.commit_edit()
        else:
            self._ui.mark_edit_invalid()

    # ── Global hotkeys ───────────────────────────────────────────────

    def _handle_global_hotkey(self, key: Key, snapshot) -> bool:
        """Returns True if the key was consumed as a global hotkey."""
        # Even unconfigured stations can use Ctrl-S to jump to Setup
        # (it's where they need to be anyway). Other hotkeys we gate.
        if key is Key.CTRL_S:
            self._ui.set_screen(Screen.SETUP)
            return True
        # Disallow other hotkeys when the station is unconfigured —
        # they don't yet have a meaningful effect, and we don't want
        # the operator to navigate away from Setup.
        if not snapshot.tx_allowed and not snapshot.emergency_override:
            return False
        if key is Key.CTRL_Q:
            self._ui.set_screen(Screen.ALLCALL)
            return True
        if key is Key.CTRL_H:
            # Per spec §6.3.4 Ctrl-H is the global jump to the
            # EMERGENCY screen. A non-radio user in a panic should
            # never have to hunt for it.
            self._ui.set_screen(Screen.EMERGENCY)
            return True
        return False

    # ── Per-screen handlers ──────────────────────────────────────────

    def _handle_screen_key(self, event: KeyEvent, snapshot) -> None:
        # Inbox detail view: Esc returns to the list, Del deletes the
        # currently-viewed row and returns. ↑/↓ are reserved for
        # future scroll-within-body, currently no-op. Other keys
        # ignored — explicitly NOT including ←/→ ring nav so the
        # operator can't accidentally lose their place by hitting
        # the cycle keys.
        if snapshot.screen is Screen.INBOX_DETAIL:
            if event.key is Key.ESC:
                self._ui.inbox_close_detail()
                return
            if event.key is Key.DELETE:
                # Delete-from-detail: matches the INBOX list pattern
                # but applied to the row currently being read. Mostly
                # useful for STORE rows the operator decides to drop
                # after reviewing the body, but works for any row
                # type. Close the detail view first so the focus
                # snaps back to the list, THEN delete the focused
                # row (the deleted row was the focused row when the
                # detail opened, so inbox_delete_focused targets the
                # right one).
                self._ui.inbox_close_detail()
                self._handle_inbox_delete(snapshot)
                return
            # ↑/↓ no-op for now (future: scroll long body)
            if event.key in (Key.UP, Key.DOWN):
                return
            # Any other key is ignored in detail view — explicit
            # "do nothing" to avoid bleeding into ring nav.
            return

        # Inbox list view (Screen.INBOX): ↑/↓/Enter/Delete operate
        # on the mailbox list. Other keys fall through to ring nav.
        if snapshot.screen is Screen.INBOX:
            if event.key is Key.UP:
                self._ui.inbox_focus_up()
                return
            if event.key is Key.DOWN:
                self._ui.inbox_focus_down()
                return
            if event.key is Key.ENTER:
                self._handle_inbox_enter(snapshot)
                return
            if event.key is Key.DELETE:
                self._handle_inbox_delete(snapshot)
                return
            # ←/→ continue to ring nav below.

        # Directed activity log (Screen.DIRECTED): ↑/↓ reserved for
        # future scroll-up-into-history (the bottom of the list is
        # always the newest). Currently no-op — operator can see
        # whatever fits on screen, no detail view in this drop.
        # ←/→ continue to ring nav.
        if snapshot.screen is Screen.DIRECTED:
            if event.key in (Key.UP, Key.DOWN, Key.ENTER):
                return

        # Compose screen (Screen.COMPOSE): a four-field editor.
        # Tab cycles TO → CMD → TEXT → SEND → TO. Type-to-edit on
        # TO/TEXT, ↑/↓ cycles the CMD dropdown, Enter on SEND fires.
        # Esc clears and returns to the previous screen. ←/→ STILL
        # navigate the ring (operators can leave Compose mid-edit
        # without losing data — the in-progress fields persist until
        # they explicitly clear).
        if snapshot.screen is Screen.COMPOSE:
            if self._handle_compose_key(event, snapshot):
                return
            # Otherwise fall through to ring nav / generic handling.

        # ALLCALL screen: three rows (HEARTBEAT / QUERY MSGS / CQ).
        # ↑/↓ cycles focus, Enter dispatches based on which row is
        # focused. ←/→ continue to ring nav so the operator can leave
        # without committing to anything. Esc is currently a no-op
        # (consistent with INBOX list — no edit state to discard).
        if snapshot.screen is Screen.ALLCALL:
            if self._handle_allcall_key(event, snapshot):
                return

        # HB_MODE_SELECT sub-screen: 4-row dropdown over the HbMode
        # values. ↑/↓ cycle, Enter commits + returns, Esc cancels +
        # returns. ←/→ also commit + return (consistent with ring
        # nav being a "leave this modal" gesture). All other keys
        # ignored so the operator can't accidentally bleed into
        # other behavior.
        if snapshot.screen is Screen.HB_MODE_SELECT:
            if self._handle_hb_mode_select_key(event, snapshot):
                return
            # Block ring nav from inside the modal — modals don't
            # cycle into the next ring screen. ←/→ commit + return.
            if event.key in (Key.LEFT, Key.RIGHT):
                self._ui.close_hb_mode_select(commit=True)
                return
            # Any other key: ignore.
            return

        # Ring nav with ← / → (locked when unconfigured).
        if event.key is Key.LEFT:
            if self._ring_locked(snapshot):
                return
            self._ui.retreat_ring()
            return
        if event.key is Key.RIGHT:
            if self._ring_locked(snapshot):
                return
            self._ui.advance_ring()
            return

        # Field focus cycling (Tab / Shift-Tab — not implementing
        # Shift-Tab in Step 3 since it requires us to track Shift in the
        # router; Tab forward is enough for the small Setup field set).
        if event.key is Key.TAB:
            self._ui.cycle_focus()
            return

        # Activation
        if event.key is Key.ENTER:
            self._handle_enter(snapshot)
            return

        # Type-to-edit: if a printable character is pressed while a
        # focused editable field is selected, automatically enter edit
        # mode and consume the character. This matches the mental model
        # that "Tab to a field, then type" works, instead of forcing the
        # operator to remember an explicit Enter to begin editing.
        if event.char is not None and snapshot.screen is Screen.SETUP:
            field = self._ui.focused_field_name()
            # Groups field accepts type-to-edit just like the other
            # text fields. The router's edit-key handler uppercases
            # by default (correct for group names like '@EMCOMM');
            # _commit_edit hands the raw buffer to the config
            # validator which splits on commas, validates each entry,
            # and rejects malformed input with an amber warn.
            if field in ("callsign", "grid", "groups", "units", "freq_hz"):
                self._ui.begin_edit(field)
                # Replace the prefilled buffer with the typed character —
                # the operator clearly wants to overwrite, not append.
                # (begin_edit pre-filled with current value; we clear it.)
                while self._ui.edit_buffer():
                    self._ui.edit_backspace()
                self._ui.edit_append(event.char)
                return

    def _ring_locked(self, snapshot) -> bool:
        """Ring navigation is locked when station is unconfigured AND
        emergency bypass hasn't been activated."""
        return not snapshot.tx_allowed and not snapshot.emergency_override

    def _handle_enter(self, snapshot) -> None:
        """Enter on the focused element of the current screen."""
        if snapshot.screen is Screen.SETUP:
            field = self._ui.focused_field_name()
            if field == "emergency_bypass":
                self._emergency_bypass()
                return
            if field == "radio":
                # Radio is a cycling selector — Enter advances to the
                # next radio_id in the registry. The cycle callback
                # writes config.toml and updates UIState. NOT a text
                # edit (no keyboard buffer).
                if self._cycle_radio is not None:
                    self._cycle_radio()
                return
            if field in ("callsign", "grid", "groups", "units", "freq_hz"):
                self._ui.begin_edit(field)
                return
        # Other screens have no Enter binding in Step 3.

    def _handle_inbox_enter(self, snapshot) -> None:
        """Enter on the focused inbox row → open detail-view + mark READ.

        Side effects:
          1. UIState.inbox_open_detail() returns the row id and
             transitions screen → INBOX_DETAIL.
          2. If the row was UNREAD, persist mark_read via the daemon
             callback (mailbox UPDATE) and update the in-memory cache
             via mark_inbox_row_read_locally so the UI reflects the
             change immediately on return to the list.

        If the inbox is empty (open_detail returns None), this is a
        no-op — the operator pressed Enter on a blank list.
        """
        row_id = self._ui.inbox_open_detail()
        if row_id is None:
            return
        # Look up the row to decide whether to mark READ — only
        # UNREAD rows need the transition (READ → READ is a no-op
        # but writes to disk, which we want to avoid on re-opens).
        focused_row = None
        for row in snapshot.inbox_messages:
            if row.id == row_id:
                focused_row = row
                break
        if focused_row is None or focused_row.is_read:
            return
        # Update the persistent store via the daemon callback. If
        # the callback is None (test harness with no mailbox) we
        # still update the local UI cache so the UI is consistent.
        try:
            if self._mark_inbox_read is not None:
                self._mark_inbox_read(row_id)
        except Exception:
            _log.exception("mark_inbox_read raised on row id=%d", row_id)
        self._ui.mark_inbox_row_read_locally(row_id)

    def _handle_inbox_delete(self, snapshot) -> None:
        """Delete on the focused inbox row → hard-delete + UI dropout.

        Side effects:
          1. UIState.inbox_delete_focused() returns the row id and
             removes it from the in-memory cache so the UI updates
             instantly. Focus index is clamped (last-row → new last
             row, empty list → focus 0).
          2. The daemon's delete_inbox_row callback removes the row
             from inbox.db permanently. Hard delete — no recovery
             via SQL after this. If the callback is None (test
             harness), the in-memory drop still happens; the row
             will reappear on the next periodic refresh from disk.

        If the inbox is empty (delete_focused returns None), this is
        a no-op — operator pressed Delete on a blank list.

        Note: no confirmation prompt (the operator chose this binding
        explicitly). If we ever observe accidental deletes on-air
        we can add a "press again to confirm" debounce, but starting
        without it keeps the keypath simple.
        """
        row_id = self._ui.inbox_delete_focused()
        if row_id is None:
            return
        try:
            if self._delete_inbox_row is not None:
                self._delete_inbox_row(row_id)
        except Exception:
            _log.exception("delete_inbox_row raised on row id=%d", row_id)

    def _handle_compose_key(self, event: KeyEvent, snapshot) -> bool:
        """Dispatch a key on the COMPOSE screen. Returns True if handled.

        The handler dispatches based on (focused_field, key) pairs:

        Always-handled keys (any focused field):
          - Tab → cycle_focus (dynamic — TO → CMD → [FOR for MSG TO] → TEXT → SEND)
          - Esc → compose_clear, return to previous screen
          - ↑/↓ on TO → cycle heard-list dropdown (wraps)
          - ↑/↓ on CMD → cycle the dropdown enum
          - ↑/↓ on FOR → cycle heard-list dropdown (wraps)

        Field-specific keys:
          - TO/FOR/TEXT focused, printable char → append to value
            (typing on TO/FOR clears the heard-index marker so the
            renderer drops the HEARD-age color)
          - TO/FOR/TEXT focused, Backspace → drop last char
          - SEND focused, Enter → build wire string and enqueue (or
            for STORE, write locally + jump to INBOX); clear and exit

        Returning False means the router falls through to ring-nav
        (← / → still navigate even from COMPOSE — the in-progress
        fields are preserved, so leaving and coming back is safe).
        """
        focused = snapshot.compose_focused_field

        if event.key is Key.TAB:
            self._ui.cycle_focus()
            return True

        if event.key is Key.ESC:
            # Esc clears the compose fields but stays on COMPOSE —
            # the operator can navigate away with ← / → if they want.
            # We don't auto-retreat-ring because Compose is in the
            # main ring at index 4, so retreat_ring would send them
            # to INBOX which usually isn't where they came from
            # (most likely they came from HEARD via repeated →).
            self._ui.compose_clear()
            return True

        # ↑/↓ on TO field cycles the heard-list dropdown.
        if focused == "compose_to":
            if event.key is Key.UP:
                self._ui.compose_to_cycle_heard_prev()
                return True
            if event.key is Key.DOWN:
                self._ui.compose_to_cycle_heard_next()
                return True
            # Type-to-edit (auto-uppercase callsigns; strip whitespace).
            if event.char is not None:
                ch = event.char.upper()
                if ch.strip():
                    self._ui.compose_set_to(snapshot.compose_to + ch)
                return True
            if event.key is Key.BACKSPACE:
                if snapshot.compose_to:
                    self._ui.compose_set_to(snapshot.compose_to[:-1])
                return True
            return False

        # ↑/↓ on CMD field cycles the dropdown.
        if focused == "compose_cmd":
            if event.key is Key.UP:
                self._ui.compose_cycle_cmd(forward=False)
                return True
            if event.key is Key.DOWN:
                self._ui.compose_cycle_cmd(forward=True)
                return True
            # Other keys on CMD field don't do anything on the field
            # itself — fall through so ring nav etc. still works.
            return False

        # FOR field: same UX as TO. Only present in the focus cycle
        # when CMD is MSG_TO, but we handle it defensively here so
        # any stray Enter doesn't fall through unexpectedly.
        if focused == "compose_for":
            if event.key is Key.UP:
                self._ui.compose_for_cycle_heard_prev()
                return True
            if event.key is Key.DOWN:
                self._ui.compose_for_cycle_heard_next()
                return True
            if event.char is not None:
                ch = event.char.upper()
                if ch.strip():
                    self._ui.compose_set_for(snapshot.compose_for + ch)
                return True
            if event.key is Key.BACKSPACE:
                if snapshot.compose_for:
                    self._ui.compose_set_for(snapshot.compose_for[:-1])
                return True
            return False

        if focused == "compose_text":
            if event.char is not None:
                # JS8Call wire protocol is uppercase-only — see
                # the same rationale in ``_handle_keystroke_during_edit``
                # above. Apply the same normalisation to compose
                # bodies so on-air content stays uppercase regardless
                # of keyboard state.
                self._ui.compose_set_text(snapshot.compose_text + event.char.upper())
                return True
            if event.key is Key.SPACE:
                self._ui.compose_set_text(snapshot.compose_text + " ")
                return True
            if event.key is Key.BACKSPACE:
                if snapshot.compose_text:
                    self._ui.compose_set_text(snapshot.compose_text[:-1])
                return True
            return False

        if focused == "compose_send":
            if event.key is Key.ENTER:
                self._handle_compose_send(snapshot)
                return True
            return False

        return False

    def _handle_compose_send(self, snapshot) -> None:
        """SEND button activated → fire the compose.

        Behaviour branches on CMD:

          - **STORE** is a LOCAL action. We call ``compose_store`` to
            write a row into our mailbox for the TO callsign and jump
            to INBOX so the operator sees the row land. Nothing goes
            on the air. If ``compose_store`` returns False (validation
            failure — e.g., empty TO), we keep the compose state so
            the operator can correct it.

          - Everything else (FREE, MSG, MSG TO, AGN?, SNR?, GRID?,
            QUERY MSGS, MYLOC) is a TRANSMIT action. We call
            ``compose_send`` to build the wire and enqueue. On
            success the compose state clears and we jump to DIRECTED
            so the operator watches the activity log for replies.

        For the transmit path, the compose state is cleared
        regardless of callback success — if the queue rejects the
        message we still return the operator to a clean state rather
        than leaving half-typed content on screen. STORE is stricter:
        it leaves the compose state alone on validation failure so
        the operator can fix the TO or TEXT.

        We import ``ComposeCmd`` locally to avoid a top-level import
        cycle (state imports router types via TYPE_CHECKING, router
        imports state types lazily).
        """
        from minijs8.ui.state import ComposeCmd

        cmd = snapshot.compose_cmd

        if cmd is ComposeCmd.STORE:
            ok = True
            try:
                if self._compose_store is not None:
                    ok = bool(self._compose_store(
                        snapshot.compose_to,
                        snapshot.compose_text,
                    ))
            except Exception:
                _log.exception(
                    "compose_store raised; leaving compose state intact"
                )
                ok = False
            if ok:
                self._ui.compose_clear()
                # Jump to INBOX so the operator sees the stored row.
                self._ui.set_screen(Screen.INBOX)
            return

        # Transmit path (everything else).
        try:
            if self._compose_send is not None:
                self._compose_send(
                    snapshot.compose_to,
                    snapshot.compose_cmd,
                    snapshot.compose_text,
                    snapshot.compose_for,
                )
        except Exception:
            _log.exception("compose_send raised; UI state will be cleared anyway")
        self._ui.compose_clear()
        # Jump to DIRECTED so the operator sees the message they just
        # sent appear in the activity log. JS8Call's send-and-watch
        # workflow — operator gets immediate visual confirmation the
        # message went into the system, plus they're parked on the
        # screen where any reply will land.
        self._ui.set_screen(Screen.DIRECTED)

    # ── ALLCALL + HB_MODE_SELECT ────────────────────────────────────

    def _handle_allcall_key(self, event: KeyEvent, snapshot) -> bool:
        """ALLCALL screen — three-row menu (HEARTBEAT / QUERY MSGS / CQ).

        ↑/↓ cycle focus, Enter dispatches based on the focused row.
        Returns True if the key was consumed; False to let it fall
        through to ring navigation (←/→) or other defaults.

        Dispatch:
          - focus 0 (HEARTBEAT) → open HB_MODE_SELECT modal
          - focus 1 (QUERY MSGS) → fire the @ALLCALL QUERY MSGS broadcast
          - focus 2 (CQ) → fire the CQ broadcast

        For QUERY MSGS and CQ, an absent callback (router constructed
        without an outbound queue, e.g. in tests) silently no-ops —
        the focus stays where it was, no UI state changes.
        """
        if event.key is Key.UP:
            self._ui.allcall_focus_prev()
            return True
        if event.key is Key.DOWN:
            self._ui.allcall_focus_next()
            return True
        if event.key is Key.ENTER:
            focus = snapshot.allcall_focus
            if focus == 0:
                # HEARTBEAT — open the mode-select modal. The state
                # method initializes hb_select_focus to the currently-
                # active mode so the operator sees "this is what's
                # running now" when the sub-screen opens.
                self._ui.open_hb_mode_select()
            elif focus == 1:
                # QUERY MSGS — fire-and-forget broadcast. Result
                # (any held messages from peers) arrives over the
                # next few slots via the normal decode path and
                # lands in DIRECTED + INBOX.
                if self._allcall_query_msgs is not None:
                    try:
                        self._allcall_query_msgs()
                    except Exception:
                        _log.exception(
                            "allcall_query_msgs callback raised"
                        )
            elif focus == 2:
                # CQ — fire-and-forget broadcast. The callback
                # builds the wire form "CQ CQ CQ <grid>" using the
                # station's configured grid.
                if self._allcall_cq is not None:
                    try:
                        self._allcall_cq()
                    except Exception:
                        _log.exception("allcall_cq callback raised")
            return True
        return False

    def _handle_hb_mode_select_key(
        self, event: KeyEvent, snapshot
    ) -> bool:
        """HB_MODE_SELECT modal sub-screen — 4-row dropdown over
        HbMode (OFF / SINGLE / 20 MIN / 1 HR).

        ↑/↓ cycle focus, Enter commits + returns to ALLCALL, Esc
        cancels + returns to ALLCALL. Returns True if consumed.

        The commit path goes through UIState.close_hb_mode_select,
        which calls set_hb_mode under the hood — that fires the
        app's mode-change hook which then starts/stops/restarts
        the beacon thread. The router doesn't need to know any
        of that.
        """
        if event.key is Key.UP:
            self._ui.hb_select_focus_prev()
            return True
        if event.key is Key.DOWN:
            self._ui.hb_select_focus_next()
            return True
        if event.key is Key.ENTER:
            self._ui.close_hb_mode_select(commit=True)
            return True
        if event.key is Key.ESC:
            self._ui.close_hb_mode_select(commit=False)
            return True
        return False
