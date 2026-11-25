"""Thread-safe Human-in-the-Loop (HITL) approval broker for `action: ask` hooks."""
from __future__ import annotations

import contextvars
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_DEFAULT_TIMEOUT_S = 120.0

_THREAD_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "magic_approval_thread", default=""
)


@dataclass
class ApprovalOutcome:
    approved: bool
    status: str  # "approved" | "denied" | "timeout"
    note: str = ""
    approval_id: str = ""


@dataclass
class PendingApproval:
    approval_id: str
    thread_id: str
    hook_name: str
    tool_name: str
    command_preview: str
    reason: str
    risk_level: str
    created_at: float
    expires_at: float
    prompt_title: str = "Approval required"
    event: threading.Event = field(default_factory=threading.Event)
    status: str = "pending"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "thread_id": self.thread_id,
            "hook_name": self.hook_name,
            "tool_name": self.tool_name,
            "command_preview": self.command_preview,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "prompt_title": self.prompt_title,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "note": self.note,
        }


def set_thread_context(thread_id: str) -> contextvars.Token[str]:
    return _THREAD_CTX.set(str(thread_id or "").strip())


def reset_thread_context(token: contextvars.Token[str]) -> None:
    _THREAD_CTX.reset(token)


def current_thread_context() -> str:
    return _THREAD_CTX.get("")


def _default_timeout_s() -> float:
    raw = os.environ.get("MAGIC_APPROVAL_TIMEOUT_S", "").strip()
    if raw:
        try:
            return max(0.05, float(raw))
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT_S


class ApprovalBroker:
    """Tracks pending tool approvals and wakes blocked execution on resolution."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, PendingApproval] = {}
        self._subscribers: dict[int, tuple[Optional[str], Callable[[str, dict[str, Any]], None]]] = {}
        self._next_sub_id = 1
        self._approved_count = 0
        self._denied_count = 0
        self._timeout_count = 0

    def subscribe(
        self,
        callback: Callable[[str, dict[str, Any]], None],
        *,
        thread_id: Optional[str] = None,
    ) -> Callable[[], None]:
        sub_tid = str(thread_id).strip() if thread_id else None
        with self._lock:
            sid = self._next_sub_id
            self._next_sub_id += 1
            self._subscribers[sid] = (sub_tid, callback)

        def _unsub() -> None:
            with self._lock:
                self._subscribers.pop(sid, None)

        return _unsub

    def _notify(self, event_type: str, payload: dict[str, Any]) -> None:
        req_tid = str(payload.get("thread_id") or "").strip()
        with self._lock:
            targets = [
                cb
                for sub_tid, cb in self._subscribers.values()
                if sub_tid is None or not req_tid or sub_tid == req_tid
            ]
        for cb in targets:
            try:
                cb(event_type, dict(payload))
            except Exception:
                pass

    def _has_subscriber(self, thread_id: str) -> bool:
        tid = str(thread_id or "").strip()
        with self._lock:
            return any(
                sub_tid is None or not tid or sub_tid == tid
                for sub_tid, _ in self._subscribers.values()
            )

    def _expire_stale_locked(self, now: float) -> list[dict[str, Any]]:
        expired_payloads: list[dict[str, Any]] = []
        for aid in list(self._pending.keys()):
            item = self._pending[aid]
            if now >= item.expires_at:
                self._pending.pop(aid, None)
                item.status = "timeout"
                item.note = item.note or "Approval timed out"
                self._timeout_count += 1
                item.event.set()
                expired_payloads.append(item.to_dict())
        return expired_payloads

    def request_approval(
        self,
        *,
        tool_name: str,
        command_preview: str,
        reason: str = "",
        hook_name: str = "",
        risk_level: str = "medium",
        prompt_title: str = "Approve command execution?",
        thread_id: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> ApprovalOutcome:
        eff_tid = str(thread_id if thread_id is not None else current_thread_context()).strip()
        eff_timeout = max(0.05, float(timeout_s)) if timeout_s is not None else _default_timeout_s()
        now = time.time()
        aid = uuid.uuid4().hex[:12]

        if (
            os.environ.get("MAGIC_APPROVAL_AUTO_DENY_NO_CLIENT", "").strip() == "1"
            and not self._has_subscriber(eff_tid)
        ):
            with self._lock:
                self._denied_count += 1
            return ApprovalOutcome(
                approved=False,
                status="denied",
                note="No approval client attached (MAGIC_APPROVAL_AUTO_DENY_NO_CLIENT=1)",
                approval_id=aid,
            )

        item = PendingApproval(
            approval_id=aid,
            thread_id=eff_tid,
            hook_name=str(hook_name or ""),
            tool_name=str(tool_name or ""),
            command_preview=str(command_preview or "")[:1200],
            reason=str(reason or ""),
            risk_level=str(risk_level or "medium").lower(),
            prompt_title=str(prompt_title or "Approval required"),
            created_at=now,
            expires_at=now + eff_timeout,
        )

        with self._lock:
            self._pending[aid] = item
        payload = item.to_dict()
        self._notify("approval_required", payload)

        try:
            signaled = item.event.wait(timeout=eff_timeout)
        except Exception as exc:
            with self._lock:
                removed = self._pending.pop(aid, None)
                if removed is not None:
                    removed.status = "timeout"
                    removed.note = f"Approval error: {type(exc).__name__}"
                    self._timeout_count += 1
            self._notify("approval_resolved", item.to_dict())
            return ApprovalOutcome(approved=False, status="timeout", note=item.note, approval_id=aid)

        if not signaled:
            with self._lock:
                removed = self._pending.pop(aid, None)
                if removed is not None and removed.status == "pending":
                    removed.status = "timeout"
                    removed.note = f"Timed out after {int(round(eff_timeout))}s"
                    self._timeout_count += 1
                    res_payload = removed.to_dict()
                else:
                    res_payload = item.to_dict()
            if item.status == "timeout":
                self._notify("approval_resolved", res_payload)

        return ApprovalOutcome(
            approved=(item.status == "approved"),
            status=item.status if item.status in ("approved", "denied", "timeout") else "timeout",
            note=item.note,
            approval_id=aid,
        )

    def resolve(self, approval_id: str, decision: str, note: str = "") -> Optional[dict[str, Any]]:
        clean_id = str(approval_id or "").strip()
        dec = str(decision or "").strip().lower()
        if dec not in ("approve", "approved", "deny", "denied"):
            raise ValueError(f"Invalid approval decision {decision!r}; expected 'approve' or 'deny'")
        is_approve = dec in ("approve", "approved")

        with self._lock:
            expired_payloads = self._expire_stale_locked(time.time())
            item = self._pending.pop(clean_id, None)
            if item is not None:
                item.status = "approved" if is_approve else "denied"
                item.note = str(note or "")
                if is_approve:
                    self._approved_count += 1
                else:
                    self._denied_count += 1
                item.event.set()
                resolved_payload = item.to_dict()
            else:
                resolved_payload = None

        for exp in expired_payloads:
            self._notify("approval_resolved", exp)
        if resolved_payload is None:
            return None
        self._notify("approval_resolved", resolved_payload)
        return resolved_payload

    def list_pending(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            expired_payloads = self._expire_stale_locked(now)
            rows = [item.to_dict() for item in sorted(self._pending.values(), key=lambda x: x.created_at)]
        for exp in expired_payloads:
            self._notify("approval_resolved", exp)
        return rows

    def stats(self) -> dict[str, Any]:
        with self._lock:
            expired_payloads = self._expire_stale_locked(time.time())
            snap = {
                "pending": len(self._pending),
                "approved": self._approved_count,
                "denied": self._denied_count,
                "timed_out": self._timeout_count,
            }
        for exp in expired_payloads:
            self._notify("approval_resolved", exp)
        return snap

    def reset_for_tests(self) -> None:
        with self._lock:
            for item in self._pending.values():
                item.status = "denied"
                item.note = "reset"
                item.event.set()
            self._pending.clear()
            self._subscribers.clear()
            self._approved_count = 0
            self._denied_count = 0
            self._timeout_count = 0


_MANAGER_LOCK = threading.Lock()
_MANAGER: Optional[ApprovalBroker] = None


def get_manager() -> ApprovalBroker:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ApprovalBroker()
        return _MANAGER


def subscribe(callback: Callable[[str, dict[str, Any]], None], *, thread_id: Optional[str] = None) -> Callable[[], None]:
    return get_manager().subscribe(callback, thread_id=thread_id)


def request_approval(**kwargs: Any) -> ApprovalOutcome:
    return get_manager().request_approval(**kwargs)


def resolve(approval_id: str, decision: str, note: str = "") -> Optional[dict[str, Any]]:
    return get_manager().resolve(approval_id, decision, note=note)


def list_pending() -> list[dict[str, Any]]:
    return get_manager().list_pending()


def stats() -> dict[str, Any]:
    return get_manager().stats()
