"""Bounded reduction; every candidate must pass the independent input validator."""
from pathlib import Path
from .execution import ExecutionError


def minimize(data: str, accepts_failure, proposals=None, max_attempts=40) -> str:
    import time
    deadline = time.monotonic() + 60
    best = data
    attempts = 0
    # Structured reductions precede generic deletions, which validators guard.
    candidates = proposals(data) if proposals else []
    for candidate in candidates:
        if attempts >= max_attempts or time.monotonic() >= deadline:
            break
        attempts += 1
        if candidate and len(candidate) < len(best) and accepts_failure(candidate):
            best = candidate
    chunks = best.splitlines(keepends=True)
    width = max(1, len(chunks) // 2)
    while width and attempts < max_attempts and time.monotonic() < deadline:
        index = 0
        changed = False
        while index < len(chunks) and attempts < max_attempts and time.monotonic() < deadline:
            candidate = ''.join(chunks[:index] + chunks[index + width:])
            attempts += 1
            if candidate.strip() and accepts_failure(candidate):
                chunks = candidate.splitlines(keepends=True)
                best = candidate
                changed = True
            else:
                index += width
        if not changed:
            width //= 2
    return best
