"""Bounded backward scanning of append-only session transcripts.

One agentic turn can emit megabytes of tool output after its user message,
pushing the turn boundary beyond any fixed tail window; turn timing and
reply judging then degrade silently. These helpers read the live tail
first and then walk earlier blocks backwards until the caller's turn
boundary appears, with a hard cap so worst-case cost stays bounded.
Blocks are line-aligned and partial trailing lines are withheld until the
next append completes them, so incremental cursors never lose records.
"""

TAIL_BYTES = 2 * 1024 * 1024
BLOCK_BYTES = 4 * 1024 * 1024
BACKSCAN_CAP_BYTES = 64 * 1024 * 1024


def scan_blocks(size, window=TAIL_BYTES, cap=None):
    """Yield (start, end) read ranges: the live tail first, then earlier
    contiguous blocks walking backwards; stops at the cap or file start."""
    cap = BACKSCAN_CAP_BYTES if cap is None else cap
    end = size
    start = max(0, end - window)
    yield start, end
    limit = max(0, size - cap)
    while start > limit:
        end, start = start, max(limit, start - BLOCK_BYTES)
        yield start, end


def complete_lines(data, drop_first):
    """Complete JSONL lines from one block read. A trailing partial line is
    withheld until the file supplies its remainder; a mid-file block starts
    inside a line, so its first fragment is always dropped."""
    lines = data.splitlines()
    if drop_first and lines:
        lines = lines[1:]
    if data and not data.endswith(b'\n') and lines:
        lines.pop()
    return lines


def cursor_after(data, start):
    """File offset just past the last complete line of a block read that
    began at `start`; a withheld partial line stays unconsumed."""
    last = data.rfind(b'\n')
    return start + last + 1 if last >= 0 else start
