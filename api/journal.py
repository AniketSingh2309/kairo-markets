"""Trade journal over the paper account: round-trip trades, your notes and tags, and the statistics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import deps
from core import journal as jr
from core.store import Store

router = APIRouter(prefix="/journal", tags=["journal"])


class NoteIn(BaseModel):
    setup: str = Field("", max_length=40)
    mistakes: list[str] = Field(default_factory=list, max_length=10)
    note: str = Field("", max_length=2000)
    rating: int | None = Field(None, ge=1, le=5)


def _trades(store: Store) -> list[dict[str, Any]]:
    supers = store.supers(("pending", "active", "done", "cancelled"))
    stops = {s["entry_order_id"]: s["init_stop"] or s["stop"] for s in supers if s["entry_order_id"]}
    return jr.round_trips(store.paper_fills(), stops)


@router.get("")
async def journal(store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    trades, notes = _trades(store), store.journal_notes()
    for t in trades:
        n = notes.get(t["key"])
        t["notes"] = {k: n[k] for k in ("setup", "mistakes", "note", "rating")} if n else None
    return {"trades": trades, "stats": jr.stats(trades, notes), "setups": jr.SETUPS, "mistakes": jr.MISTAKES,
            "about": "Paper trades, before brokerage and taxes. A trade runs from flat to flat; R = result ÷ the risk set by the initial stop."}


@router.put("/{key:path}")
async def save_note(key: str, body: NoteIn, store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    if not any(t["key"] == key for t in _trades(store)):
        raise HTTPException(status_code=404, detail="no such trade")
    store.save_journal_note(key, body.setup.strip(), [m for m in body.mistakes if m.strip()][:10], body.note.strip(), body.rating)
    return {"saved": key}
