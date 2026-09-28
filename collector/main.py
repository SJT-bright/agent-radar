#!/usr/bin/python3
"""Local-only bounded collectors. Emits one complete JSON snapshot per line."""
import argparse
import json
import time
import sys
from concurrent.futures import ThreadPoolExecutor

import codex_adapter
import desktop_adapters
import autoclaw_adapter
import extended_adapters
import cline_adapter


# Leave most of the ten-second recovery budget to verified navigation/input.
# Adapters reuse their read-only caches; a slow scan never overlaps the next one.
POLL_INTERVAL_SECONDS = 1.0


def snapshot():
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [(name, executor.submit(module.collect)) for name, module in
                   [("Codex / Claude", codex_adapter), ("ZCode / WorkBuddy", desktop_adapters),
                    ("AutoClaw", autoclaw_adapter), ("Qoder / Grok", extended_adapters),
                    ("Cline", cline_adapter)]]
        for name, future in futures:
            try:
                rows.extend(future.result())
            except Exception as exc:
                errors.append(name + " 采集失败 (" + type(exc).__name__ + ")")
    errors.extend(codex_adapter._collector.last_errors)
    if hasattr(desktop_adapters, "diagnostics"):
        errors.extend(desktop_adapters.diagnostics())
    errors.extend(autoclaw_adapter.diagnostics())
    errors.extend(extended_adapters.diagnostics())
    errors.extend(cline_adapter.diagnostics())
    unique = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        row.setdefault("target", "")
        row.setdefault("project", "")
        unique[row["id"]] = row
    return {"sessions": list(unique.values()), "errors": errors, "collected_at": time.time()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    while True:
        started = time.monotonic()
        print(json.dumps(snapshot(), ensure_ascii=False), flush=True)
        if not args.watch:
            break
        time.sleep(max(0.1, POLL_INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
