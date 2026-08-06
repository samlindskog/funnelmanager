"""The mail-archive walk's two-mode watermark state machine (source API faked)."""

import asyncio
from unittest.mock import patch

from app.sync.base import SyncResult
from app.sync import sources


class _NullSession:
    async def flush(self):
        pass

    async def execute(self, stmt):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()

    def add(self, obj):
        pass


def _msg(i, date):
    return {"gmail_id": f"g{i}", "internal_date": date, "from_addr": "x@y.z", "label_ids": "[]"}


def _run_walk(pages, state, pages_per_cycle=200, head_pages=2):
    """Drive _walk_account against a canned page sequence; returns (state, calls)."""
    calls = []

    async def fake_get_json(base_url, audience, path, params=None):
        calls.append(dict(params or {}))
        page = int(params["page"])
        return {"messages": pages[page - 1] if page <= len(pages) else []}

    test_cfg = sources.get_settings().model_copy(
        update={"mail_pages_per_cycle": pages_per_cycle, "mail_head_pages": head_pages}
    )

    async def go():
        with (
            patch.object(sources, "_get_json", fake_get_json),
            patch.object(sources, "get_settings", lambda: test_cfg),
        ):
            return await sources._walk_account(
                _NullSession(), 1, "me@box.com", "sam", state, SyncResult()
            )

    return asyncio.run(go()), calls


def test_walk_requests_all_labels_and_deleted():
    _, calls = _run_walk([[_msg(1, "2026-08-01T00:00:00Z")]], {})
    assert calls and all(c["label"] == "ALL" and c["include_deleted"] == "true" for c in calls)


def test_initial_backfill_completes_and_sets_marker():
    pages = [[_msg(1, "2026-08-02T00:00:00Z")], [_msg(2, "2026-08-01T00:00:00Z")]]
    state, _ = _run_walk(pages, {})
    assert state == {"marker": "2026-08-02T00:00:00Z"}


def test_capped_backfill_resumes_and_holds_marker():
    pages = [[_msg(1, "2026-08-03T00:00:00Z")], [_msg(2, "2026-08-02T00:00:00Z")], [_msg(3, "2026-08-01T00:00:00Z")]]
    state, _ = _run_walk(pages, {}, pages_per_cycle=1)
    assert "marker" not in state
    assert state["resume_page"] == 2
    assert state["newest_candidate"] == "2026-08-03T00:00:00Z"
    # Next cycle resumes from page 2 and finishes; the head candidate becomes the marker.
    state2, calls2 = _run_walk(pages, state, pages_per_cycle=5)
    assert calls2[0]["page"] == 2
    assert state2 == {"marker": "2026-08-03T00:00:00Z"}


def test_incremental_stops_past_watermark_and_advances_marker():
    pages = [
        [_msg(1, "2026-08-05T00:00:00Z")],
        [_msg(2, "2026-08-04T00:00:00Z")],
        [_msg(3, "2026-07-01T00:00:00Z")],  # older than the marker
        [_msg(4, "2026-06-01T00:00:00Z")],
    ]
    state, calls = _run_walk(pages, {"marker": "2026-08-01T00:00:00Z"}, head_pages=2)
    assert state == {"marker": "2026-08-05T00:00:00Z"}
    # walked until past-watermark at >= head_pages, not the whole archive
    assert len(calls) == 3


def test_capped_incremental_holds_marker():
    pages = [[_msg(i, f"2026-08-0{9 - i}T00:00:00Z")] for i in range(1, 6)]
    state, _ = _run_walk(pages, {"marker": "2026-01-01T00:00:00Z"}, pages_per_cycle=2)
    assert state["marker"] == "2026-01-01T00:00:00Z"  # held, not advanced past a gap


def test_legacy_string_state_is_upgraded():
    async def fake_get_json(base_url, audience, path, params=None):
        if path.endswith("/accounts"):
            return [{"id": 1, "email": "me@box.com", "connected_by": "sam"}]
        return {"messages": []}

    async def fake_get_watermark(session, source):
        return {"accounts": {"1": "2026-08-01T00:00:00Z"}}  # old plain-marker shape

    async def go():
        with (
            patch.object(sources, "_get_json", fake_get_json),
            patch.object(sources, "get_watermark", fake_get_watermark),
        ):
            _, wm = await sources.sync_mail_messages(_NullSession())
        return wm

    wm = asyncio.run(go())
    assert wm["accounts"]["1"]["marker"] == "2026-08-01T00:00:00Z"
