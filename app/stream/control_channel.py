"""Sends control messages (touches, keys, scroll) to the phone without ever blocking the GUI thread.

Guarantees
- ORDER: messages leave in the order they were queued, except that a pending touch MOVE may be replaced by a newer MOVE
  of the same finger (see below). Nothing else is ever reordered.
- NO LOSS of state changes: touch DOWN, touch UP, key presses, scroll and every other message are never dropped,
  merged or reordered. In particular an UP can never be discarded to make room.
- MOVE COALESCING: a MOVE only says "the finger is now here". If a MOVE of finger N is still waiting in the queue and a newer
  MOVE of finger N arrives, the waiting one is updated in place (the phone only needs the latest position). A MOVE is never
  merged across a DOWN or UP of the same finger: once DOWN/UP of N is queued, the next MOVE of N starts a new entry.
  So at most one MOVE per finger can be waiting, which bounds the MOVE traffic by the number of fingers, however fast
  the mouse produces them.
- BOUNDED: if the phone stops reading, a send that takes longer than `stall_seconds` marks the channel stalled; so does having
  more than `max_pending` state-changing messages waiting. A stalled channel is closed and `on_stalled(reason)` is called,
  so the session can end visibly instead of growing a queue forever or silently dropping a release.
- GRACEFUL CLOSE: `close(graceful=True)` lets everything already queued leave first, and adds a synthetic UP for any finger
  that is still down, so a normal disconnect does not leave a finger pressed on the phone. (If the connection is already dead
  nothing can be delivered; what Android does with such a finger is up to Android.)
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
from collections import deque
from typing import Callable

from . import protocol

log = logging.getLogger(__name__)

MAX_PENDING = 256            # state-changing messages that may wait (MOVEs are coalesced and do not count)
STALL_SECONDS = 3.0          # one send may not take longer than this


def touch_header(data: bytes) -> tuple[int, int] | None:
    """(action, pointer id) of a touch message, or None for any other message."""
    if len(data) == protocol.TOUCH_MESSAGE_SIZE and data[0] == protocol.TYPE_INJECT_TOUCH_EVENT:
        return data[1], struct.unpack_from(">q", data, 2)[0]
    return None


def release_message(last_touch: bytes) -> bytes:
    """An UP at the last known position of a finger (same size, zero pressure)."""
    message = bytearray(last_touch)
    message[1] = protocol.TOUCH_UP
    struct.pack_into(">H", message, 22, 0)
    return bytes(message)


class _Item:
    __slots__ = ("data", "pointer")

    def __init__(self, data: bytes, pointer: int | None = None) -> None:
        self.data = data
        self.pointer = pointer          # set only for a coalescable MOVE


class ControlChannel:
    """Thread-safe control sender. `send()` only queues; a writer thread does the socket work."""

    def __init__(self, sock: socket.socket, *, max_pending: int = MAX_PENDING, stall_seconds: float = STALL_SECONDS,
                 on_stalled: Callable[[str], None] | None = None) -> None:
        self._sock = sock
        self._max_pending = max_pending
        self.on_stalled = on_stalled
        self._wake = threading.Condition(threading.Lock())
        self._items: deque[_Item] = deque()
        self._moves: dict[int, _Item] = {}            # waiting MOVE of each finger (the one new MOVEs update)
        self._state_count = 0                          # waiting messages that are not coalescable MOVEs
        self._down: dict[int, bytes] = {}              # fingers that are down on the phone -> their last touch message
        self._closing = False
        self._closed = False
        self._drained = threading.Event()
        self.failure = ""                              # why the channel stopped, if it did not close normally
        self.coalesced_moves = 0
        sock.settimeout(stall_seconds)
        threading.Thread(target=self._writer, name="control-writer", daemon=True).start()
        threading.Thread(target=self._drain, name="control-drain", daemon=True).start()

    # -- properties --------------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def pending_count(self) -> int:
        with self._wake:
            return len(self._items)

    def pressed_pointers(self) -> frozenset:
        """Fingers that were put down and not yet lifted, as far as this channel has seen."""
        with self._wake:
            return frozenset(self._down)

    # -- sending -----------------------------------------------------------------------------------
    def send(self, data: bytes) -> bool:
        header = touch_header(data)
        overflow = False
        with self._wake:
            if self._closed or self._closing:
                return False
            if header is not None:
                action, pointer = header
                if action == protocol.TOUCH_MOVE:
                    if pointer in self._down:
                        self._down[pointer] = data               # latest known position (for a synthetic release)
                    waiting = self._moves.get(pointer)
                    if waiting is not None:
                        waiting.data = data
                        self.coalesced_moves += 1
                        return True
                    item = _Item(data, pointer)
                    self._moves[pointer] = item
                    self._items.append(item)
                    self._wake.notify()
                    return True
                self._moves.pop(pointer, None)                   # DOWN / UP: later MOVEs of this finger queue behind it
                if action == protocol.TOUCH_DOWN:
                    self._down[pointer] = data
                elif action == protocol.TOUCH_UP:
                    self._down.pop(pointer, None)
            self._items.append(_Item(data))
            self._state_count += 1
            if self._state_count > self._max_pending:
                overflow = True
            else:
                self._wake.notify()
        if overflow:
            self._fail(f"The phone is not keeping up: {self._max_pending} control messages are waiting")
            return False
        return True

    def key(self, keycode: int) -> None:
        self.send(protocol.encode_keycode(protocol.KEY_DOWN, keycode) + protocol.encode_keycode(protocol.KEY_UP, keycode))

    def touch(self, action: int, pointer_id: int, x: int, y: int, width: int, height: int,
              pressure: float = 1.0) -> None:
        self.send(protocol.encode_touch(action, pointer_id, x, y, width, height, pressure))

    # -- closing -----------------------------------------------------------------------------------
    def close(self, graceful: bool = False) -> None:
        """Stop sending. Graceful: send what is queued first and lift any finger that is still down."""
        with self._wake:
            if self._closed:
                return
            if graceful and not self._closing:
                for last in self._down.values():
                    self._items.append(_Item(release_message(last)))
                    self._state_count += 1
                self._down.clear()
                self._moves.clear()
                self._closing = True
                self._wake.notify_all()
                return
            if graceful:
                return
            self._closed = True
            self._items.clear()
            self._moves.clear()
            self._state_count = 0
            self._wake.notify_all()
        self._drained.set()

    def wait_drained(self, timeout: float) -> bool:
        """After close(graceful=True): True once everything queued has been handed to the socket. Never call from the
        GUI thread."""
        return self._drained.wait(timeout)

    def _fail(self, reason: str, notify: bool = True) -> None:
        with self._wake:
            if self._closed:
                return
            self._closed = True
            self.failure = reason if notify else self.failure
            self._items.clear()
            self._moves.clear()
            self._state_count = 0
            self._wake.notify_all()
        self._drained.set()
        if notify:
            log.warning("Control channel stopped: %s", reason)
            callback = self.on_stalled
            if callback is not None:
                try:
                    callback(reason)
                except Exception:                              # noqa: BLE001 - a bad callback must not kill a thread
                    log.exception("on_stalled callback failed")
        else:
            log.info("Control channel closed: %s", reason)

    # -- threads -----------------------------------------------------------------------------------
    def _writer(self) -> None:
        while True:
            with self._wake:
                while not self._items and not self._closed:
                    if self._closing:
                        self._drained.set()
                        return
                    self._wake.wait()
                if self._closed:
                    self._drained.set()
                    return
                item = self._items.popleft()
                if item.pointer is not None:
                    if self._moves.get(item.pointer) is item:
                        del self._moves[item.pointer]
                else:
                    self._state_count -= 1
                data = item.data
            try:
                self._sock.sendall(data)
            except TimeoutError:
                self._fail(f"The phone did not accept control messages for {self._sock.gettimeout():.0f} seconds")
                return
            except OSError as exc:
                self._fail(f"the connection to the phone closed ({exc})", notify=False)
                return

    def _drain(self) -> None:
        """The phone's own control messages (clipboard, ...) are not used; read and ignore them."""
        try:
            while True:
                try:
                    if not self._sock.recv(4096):
                        break
                except TimeoutError:
                    if self._closed:
                        return
        except OSError:
            pass
        self._fail("the phone closed the control connection", notify=False)
