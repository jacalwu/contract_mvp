"""Every write operation appends one immutable audit event."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.constants import Actor, new_id, now
from db.database import sql


def _serialize(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _actor_name(actor: Actor | str) -> str:
    return actor.name if isinstance(actor, Actor) else str(actor)


def log_event(
    con,
    *,
    contract_id: str,
    actor: Actor | str,
    event_type: str,
    target_id: str | None = None,
    before_value: Any = None,
    after_value: Any = None,
    at: datetime | None = None,
) -> str:
    event_id = new_id("EVT")
    sql(
        con,
        """INSERT INTO audit_events
           (event_id, contract_id, actor, event_type, target_id, before_value, after_value, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            event_id,
            contract_id,
            _actor_name(actor),
            event_type,
            target_id,
            _serialize(before_value),
            _serialize(after_value),
            at or now(),
        ],
    )
    return event_id


def list_events(contract_id: str) -> list[dict]:
    from db.database import fetch_all

    return fetch_all(
        "SELECT * FROM audit_events WHERE contract_id = ? ORDER BY created_at DESC, event_id DESC",
        [contract_id],
    )
