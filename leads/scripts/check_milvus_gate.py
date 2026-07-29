"""Standalone correctness check for the priority Milvus gate.

Exercises the real ``MilvusGate`` from ``app.milvus_client`` (no Milvus/OpenAI/
Mongo needed) to prove:
  1. an interactive query (``nice=0``) preempts queued backfills even when it
     arrives last, and FIFO holds within a nice tier;
  2. a waiter cancelled while queued does not wedge the gate;
  3. the slow (embed) stage overlaps while only the short (upsert) stage
     serializes — i.e. concurrent embeds are not gated behind each other.

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


async def main() -> None:
    await test_priority_preemption_and_fifo()
    await test_cancel_while_queued_does_not_wedge()
    await test_holder_cancel_after_handoff_does_not_lose_slot()
    await test_concurrent_embed_overlap()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
