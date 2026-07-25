"""A minimal STUB job producer — verification only, never shipped.

It implements the P0 producer contract from ``fm_runtime.job_events`` so the
`jobs` service can be exercised end-to-end before the real `search` / `agents`
producers exist:

- ``GET  /internal/jobs/v1/stream``            — NDJSON of JobEvents. Replays
  each job's whole buffered history to a late subscriber, then tails live
  events (mirrors how a real producer buffers per-job history).
- ``POST /internal/jobs/v1/{job_id}/{action}`` — pause|resume|cancel; maps onto
  the in-memory job and returns ``{"status": <new JobStatus>}``.

On startup it launches one long-running fake job (``stub-job-1``) that walks
progress 0->1 so you can watch it appear, progress, and be paused/canceled
through the jobs MCP API.

NO auth: this is a throwaway local harness, so it does not install fm_runtime.
The jobs subscriber reaches it in bare-dev passthrough (no token endpoint → no
Authorization header), which the stub ignores.

Run (from the repo root or anywhere):
    JOBS_STUB_STEP_SECONDS=2 uvicorn scripts.stub_producer:app --port 8000
or:
    python jobs/scripts/stub_producer.py           # serves on :8000
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fm_runtime.job_events import (
    JOBS_STREAM_PATH,
    JobControlAction,
    JobEvent,
    JobStatus,
)

_STEP_SECONDS = float(os.environ.get("JOBS_STUB_STEP_SECONDS", "2"))


class FakeJob:
    """One in-memory job that publishes lifecycle events as it advances."""

    def __init__(self, hub: EventHub, job_id: str, job_type: str, user: str) -> None:
        self._hub = hub
        self.job_id = job_id
        self.type = job_type
        self.user = user
        self.status = JobStatus.QUEUED
        self.progress = 0.0
        self._resume = asyncio.Event()
        self._resume.set()
        self._canceled = False

    def _emit(self, **extra: object) -> None:
        self._hub.publish(
            JobEvent(
                job_id=self.job_id,
                type=self.type,
                user=self.user,
                origin="user",
                actor="mcp",
                status=self.status,
                progress=self.progress,
                **extra,
            )
        )

    async def run(self) -> None:
        self.status = JobStatus.QUEUED
        self._emit()
        self.status = JobStatus.RUNNING
        self._emit()
        while self.progress < 1.0 and not self._canceled:
            await self._resume.wait()  # blocks while paused
            if self._canceled:
                break
            self.progress = round(min(1.0, self.progress + 0.1), 2)
            self.status = JobStatus.RUNNING
            self._emit()
            await asyncio.sleep(_STEP_SECONDS)
        if self._canceled:
            self.status = JobStatus.CANCELED
            self._emit(exit_status="canceled by control")
        else:
            self.status = JobStatus.COMPLETED
            self.progress = 1.0
            self._emit(exit_status="ok")

    def control(self, action: JobControlAction) -> JobStatus:
        if self.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
            return self.status  # idempotent no-op on a finished job
        if action is JobControlAction.PAUSE:
            self.status = JobStatus.PAUSED
            self._resume.clear()
            self._emit()
        elif action is JobControlAction.RESUME:
            self.status = JobStatus.RUNNING
            self._resume.set()
            self._emit()
        elif action is JobControlAction.CANCEL:
            self._canceled = True
            self._resume.set()  # unblock the loop so it can finish
        return self.status


class EventHub:
    """Fan-out with per-job replay history (what a real producer buffers)."""

    def __init__(self) -> None:
        self._history: list[dict] = []
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, event: JobEvent) -> None:
        data = event.to_dict()
        self._history.append(data)
        for queue in self._subscribers:
            queue.put_nowait(data)

    async def stream(self) -> AsyncIterator[bytes]:
        queue: asyncio.Queue = asyncio.Queue()
        # Replay history first so a late subscriber sees the whole lifecycle.
        for data in list(self._history):
            queue.put_nowait(data)
        self._subscribers.add(queue)
        try:
            while True:
                data = await queue.get()
                yield (JobEvent.from_dict(data).to_json_line()).encode()
        finally:
            self._subscribers.discard(queue)


hub = EventHub()
jobs: dict[str, FakeJob] = {}
app = FastAPI(title="stub-producer")


@app.on_event("startup")
async def _start() -> None:
    job = FakeJob(hub, "stub-job-1", "apollo_search", "alice")
    jobs[job.job_id] = job
    asyncio.create_task(job.run())


@app.get(JOBS_STREAM_PATH)
async def stream() -> StreamingResponse:
    return StreamingResponse(hub.stream(), media_type="application/x-ndjson")


@app.post("/internal/jobs/v1/{job_id}/{action}")
async def control(job_id: str, action: str) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"detail": f"no job {job_id}"}, status_code=404)
    try:
        parsed = JobControlAction.coerce(action)
    except ValueError:
        return JSONResponse({"detail": f"bad action {action}"}, status_code=400)
    new_status = job.control(parsed)
    return JSONResponse({"status": new_status.value})


if __name__ == "__main__":
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host="127.0.0.1", port=8000)
