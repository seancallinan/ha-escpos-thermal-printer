"""Shared helpers for the buffering byte-sink transports.

The serial and BLE transports both coalesce python-escpos' many small
``_raw()`` calls into one buffer and then release it to the wire in
size-bounded chunks with a pause between them. The splitting rule — and in
particular "no trailing pause after the final chunk" — is easy to get subtly
wrong in each copy, so it lives here once.
"""

from __future__ import annotations

from collections.abc import Iterator


def iter_chunks(data: bytes, chunk_size: int) -> Iterator[tuple[bytes, bool]]:
    """Split ``data`` into ``chunk_size`` pieces, flagging the final one.

    Yields ``(chunk, is_last)``. A ``chunk_size`` of 0 or less (or a payload
    that already fits) yields the whole payload as a single final chunk, so
    callers can use one loop for both the chunked and unchunked cases.

    The ``is_last`` flag exists so callers skip the inter-chunk delay after
    the final write — otherwise every print would pay an extra sleep, which
    on a slow BLE link is a visible pause before the paper stops moving.
    """
    if not data:
        return
    if chunk_size <= 0 or len(data) <= chunk_size:
        yield data, True
        return
    last_start = ((len(data) - 1) // chunk_size) * chunk_size
    for start in range(0, len(data), chunk_size):
        yield data[start : start + chunk_size], start == last_start
