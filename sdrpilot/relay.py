"""Dashboard chat -> a Claude chat. The MCP server can't push into a conversation, so messages typed on the dashboard
wait here until Claude next touches SDR Pilot: every tool result carries them, and dashboard_listen blocks until
one arrives (for "take instructions from the dashboard" mode)."""
from __future__ import annotations

import threading
import time


class ChatRelay:
    def __init__(self, board):
        self.board = board
        self.cv = threading.Condition()
        self.pending: list[dict] = []
        self.report_requested = 0.0  # the dashboard's "report" button: answer before doing anything else

    def post(self, text: str) -> dict:
        listening = self.board.chat_state.get("listening_until", 0) > time.time()
        mid = self.board.chat_add("user", text, status="queued")
        with self.cv:
            self.pending.append({"id": mid, "t": time.time(), "text": text})
            self.cv.notify_all()
        return {"id": mid, "listening": listening}

    def take(self) -> list[dict]:
        with self.cv:
            msgs, self.pending = self.pending, []
        for m in msgs:
            self.board.chat_update(m["id"], status="delivered")
        return [{"sent": time.strftime("%H:%M:%S", time.localtime(m["t"])), "text": m["text"]} for m in msgs]

    def has_pending(self) -> bool:
        with self.cv:
            return bool(self.pending)

    def wait(self, timeout: float) -> list[dict]:
        end = time.time() + timeout
        self.board.set_chat_state(listening_until=end + 5)
        try:
            with self.cv:
                self.cv.wait_for(lambda: bool(self.pending), timeout)
            return self.take()
        finally:
            # stays "listening" for a few seconds so the dashboard doesn't flicker between calls
            self.board.set_chat_state(listening_until=time.time() + 8)
