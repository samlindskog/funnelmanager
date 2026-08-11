"""Standalone correctness check for the priority Milvus gate.

Exercises the real ``MilvusGate`` from ``app.milvus_client`` (no Milvus/OpenAI/
Mongo needed) to prove:
  1. an interactive query (``nice=0``) preempts queued backfills even when it
     arrives last, and FIFO holds within a nice tier;
  2. a waiter cancelled while queued does not wedge the gate;
  3. a waiter cancelled just after being handed the slot passes it on;
  4. the slow (embed) stage overlaps while only the short (upsert) stage
     serializes — i.e. concurrent embeds are not gated behind each other;
  5. bounded-skip fairness: a sustained stream of nice=0 acquisitions cannot
     starve a queued nice=10 writer — it runs within MAX_SKIPS hand-offs
     (this case would hang/fail against the pre-fix FIFO-priority gate);
  6. failure-safe embed fan-out: a chunk raising mid-``embed_texts`` leaves no
     task running against the closed OpenAI client and surfaces the error once.

Run from the ``leads`` dir:  python scripts/check_milvus_gate.py
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from app.milvus_client import (
    NICE_BACKFILL,
    NICE_INTERACTIVE,
    NICE_SEARCH_EMBED,
    MilvusGate,
)


async def test_priority_preemption_and_fifo() -> None:
    gate = MilvusGate()
    order: list[str] = []

    async def op(name: str, nice: int, hold: float = 0.02) -> None:
        async with gate(nice):
            order.append(name)
            await asyncio.sleep(hold)

    # A holder is mid-op. While it holds, queue backfills, then a search LAST.
    async def holder() -> None:
        async with gate(NICE_SEARCH_EMBED):
            order.append("holder")
            await asyncio.sleep(0.05)

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.01)  # ensure holder acquired first
    tasks = [
        asyncio.create_task(op("backfill-1", NICE_BACKFILL)),
        asyncio.create_task(op("backfill-2", NICE_BACKFILL)),
    ]
    await asyncio.sleep(0.005)
    tasks.append(asyncio.create_task(op("SEARCH", NICE_INTERACTIVE)))  # last, top priority
    tasks.append(asyncio.create_task(op("backfill-3", NICE_BACKFILL)))
    await asyncio.gather(h, *tasks)

    print("execution order:", order)
    assert order[0] == "holder", order
    assert order[1] == "SEARCH", f"search did not preempt: {order}"
    assert order[2:] == ["backfill-1", "backfill-2", "backfill-3"], f"FIFO broken: {order}"
    print("PASS: priority preemption + FIFO-within-nice")


async def test_cancel_while_queued_does_not_wedge() -> None:
    gate = MilvusGate()

    async def longholder() -> None:
        async with gate(NICE_SEARCH_EMBED):
            await asyncio.sleep(0.05)

    async def queued() -> None:
        async with gate(NICE_BACKFILL):
            await asyncio.sleep(0.01)

    hh = asyncio.create_task(longholder())
    await asyncio.sleep(0.01)
    cancelled = asyncio.create_task(queued())
    await asyncio.sleep(0.005)
    cancelled.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cancelled
    await hh

    got: list[str] = []
    async with gate(NICE_INTERACTIVE):
        got.append("after-cancel-ok")
    assert got == ["after-cancel-ok"]
    print("PASS: cancellation while queued does not wedge the gate")


async def test_holder_cancel_after_handoff_does_not_lose_slot() -> None:
    """If a waiter is handed the slot and then cancelled, it must pass it on."""
    gate = MilvusGate()
    ran: list[str] = []

    async def holder() -> None:
        async with gate(NICE_SEARCH_EMBED):
            await asyncio.sleep(0.02)

    async def waiter(name: str) -> None:
        async with gate(NICE_BACKFILL):
            ran.append(name)
            await asyncio.sleep(0.01)

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.005)
    a = asyncio.create_task(waiter("a"))
    b = asyncio.create_task(waiter("b"))
    await asyncio.sleep(0.005)
    # Cancel 'a' the instant the holder releases (about to hand the slot to 'a').
    await asyncio.sleep(0.02)
    a.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await a
    await asyncio.wait_for(b, timeout=1.0)
    # Gate is still usable afterwards regardless of the race outcome.
    async with gate(NICE_INTERACTIVE):
        ran.append("final")
    assert "b" in ran and "final" in ran, ran
    print("PASS: cancel around hand-off keeps the gate live (b + final ran)")


async def test_concurrent_embed_overlap() -> None:
    """Simulate the real shape: slow lock-free embed, short gated upsert.

    N batches each do a 30ms 'embed' (no gate) then a 5ms 'upsert' (gated).
    If embeds overlap, wall time ≈ one embed + N upserts, not N*(embed+upsert).
    """
    gate = MilvusGate()
    n = 6
    embed_ms = 0.03
    upsert_ms = 0.005

    async def batch() -> None:
        await asyncio.sleep(embed_ms)  # lock-free OpenAI embed
        async with gate(NICE_SEARCH_EMBED):  # short serialized Milvus upsert
            await asyncio.sleep(upsert_ms)

    start = time.perf_counter()
    await asyncio.gather(*(batch() for _ in range(n)))
    elapsed = time.perf_counter() - start

    serial = n * (embed_ms + upsert_ms)
    overlapped_ceiling = embed_ms + n * upsert_ms + 0.02  # + scheduling slack
    print(f"concurrent-embed wall={elapsed*1000:.1f}ms serial-would-be={serial*1000:.1f}ms")
    assert elapsed < serial * 0.6, f"embeds did not overlap: {elapsed:.3f}s vs serial {serial:.3f}s"
    assert elapsed < overlapped_ceiling, f"slower than overlap ceiling: {elapsed:.3f}s"
    print("PASS: concurrent embeds overlap; only upsert serializes")


async def test_bounded_skip_prevents_starvation() -> None:
    """A sustained flood of nice=0 readers must not starve a queued nice=10 writer.

    Reproduces the bug-hunter finding (20 concurrent nice=0 workers starved a
    pending nice=10 writer forever). Against the pre-fix gate the writer never
    runs and ``wait_for`` below times out; with bounded-skip it runs within
    ``max_skips`` hand-offs.
    """
    max_skips = 8
    gate = MilvusGate(max_skips=max_skips)
    order: list[str] = []
    writer_done = asyncio.Event()

    async def writer() -> None:
        async with gate(NICE_SEARCH_EMBED):
            order.append("WRITER")
        writer_done.set()

    async def reader_loop(i: int) -> None:
        spins = 0
        while not writer_done.is_set():
            async with gate(NICE_INTERACTIVE):
                order.append(f"r{i}")
            spins += 1
            await asyncio.sleep(0)  # yield so peers re-queue (sustained stream)
            if spins > 5000:  # safety valve against a hang if the fix regresses
                break

    # Hold the gate so the writer and readers must queue behind a running op.
    async def holder() -> None:
        async with gate(NICE_SEARCH_EMBED):
            await asyncio.sleep(0.01)

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.002)
    w = asyncio.create_task(writer())
    await asyncio.sleep(0.001)  # writer queues first (lowest seq)
    readers = [asyncio.create_task(reader_loop(i)) for i in range(20)]

    try:
        await asyncio.wait_for(w, timeout=2.0)
    except asyncio.TimeoutError:  # pragma: no cover - only if the fix regresses
        writer_done.set()
        for task in readers:
            task.cancel()
        raise AssertionError("writer was starved (bounded-skip regressed)")
    finally:
        await asyncio.gather(h, *readers, return_exceptions=True)

    interactive_before = order.index("WRITER")
    assert interactive_before <= max_skips + 1, (
        f"writer ran after {interactive_before} interactive ops, "
        f"exceeding bound {max_skips + 1}"
    )
    print(
        "PASS: bounded-skip serves the writer after "
        f"{interactive_before} interactive ops (bound {max_skips + 1}), "
        "despite a sustained nice=0 flood"
    )


async def test_embed_fan_out_failure_is_clean() -> None:
    """A chunk raising mid-fan-out must not orphan tasks against a closed client.

    Mocks ``AsyncOpenAI`` so one chunk raises while siblings are slow; asserts
    every chunk task settled before the client context exited, the error
    surfaced exactly once, and the client was closed after all tasks finished.
    """
    import app.embeddings as embeddings

    events: list[str] = []
    client_closed = asyncio.Event()

    class _FakeItem:
        def __init__(self, index: int, embedding: list[float]) -> None:
            self.index = index
            self.embedding = embedding

    class _FakeResp:
        def __init__(self, data: list[_FakeItem]) -> None:
            self.data = data

    class _FakeEmbeddings:
        def __init__(self, client: "_FakeClient") -> None:
            self._client = client

        async def create(self, *, model, input, dimensions, encoding_format=None):  # noqa: A002
            # First chunk fails fast; the rest are slow so they are still in
            # flight when the failure would otherwise tear the client down.
            if input and input[0] == "boom":
                events.append("chunk-error")
                raise RuntimeError("simulated embed failure")
            await asyncio.sleep(0.05)
            assert not self._client.closed, "sibling ran against a CLOSED client"
            events.append("chunk-ok")
            # Return list embeddings; embed_texts' _decode_embedding passes an
            # already-decoded list through unchanged (the base64 path is exercised
            # by the embeddings unit test, not this gate-fairness check).
            return _FakeResp([_FakeItem(i, [float(len(t))]) for i, t in enumerate(input)])

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            self.embeddings = _FakeEmbeddings(self)

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc) -> bool:
            self.closed = True
            client_closed.set()
            return False

    class _Cfg:
        openai_configured = True
        openai_api_key = "x"
        openai_embedding_model = "m"
        openai_embedding_dimensions = 1
        openai_max_retries = 6
        embed_concurrency = 8

    original = embeddings.AsyncOpenAI
    embeddings.AsyncOpenAI = _FakeClient  # type: ignore[assignment]
    try:
        # 3 chunks of 64 (>64 forces multiple): chunk 0 fails, chunks 1-2 are slow.
        texts = ["boom"] + [f"t{i}" for i in range(64 * 3 - 1)]
        raised = False
        try:
            await embeddings.embed_texts(texts, settings=_Cfg())  # type: ignore[arg-type]
        except RuntimeError as exc:
            raised = True
            assert "simulated embed failure" in str(exc)
    finally:
        embeddings.AsyncOpenAI = original  # type: ignore[assignment]

    assert raised, "embed_texts should re-raise the chunk error"
    assert client_closed.is_set(), "client was never closed"
    # Both surviving chunks must have completed (proving they were awaited before
    # the client closed), and the error was observed once.
    assert events.count("chunk-error") == 1, events
    assert events.count("chunk-ok") == 2, events
    print("PASS: failed embed fan-out closes cleanly (no task outlives the client)")


async def main() -> None:
    await test_priority_preemption_and_fifo()
    await test_cancel_while_queued_does_not_wedge()
    await test_holder_cancel_after_handoff_does_not_lose_slot()
    await test_concurrent_embed_overlap()
    await test_bounded_skip_prevents_starvation()
    await test_embed_fan_out_failure_is_clean()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
