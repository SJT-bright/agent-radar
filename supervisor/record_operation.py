"""Accept one metadata-only controller receipt on stdin; no desktop or send path."""
import json
import sys
from operation_receipts import record, support_directory


def main():
    try:
        raw = sys.stdin.buffer.read(2049)
        if len(raw) > 2048:
            return 1
        fields = json.loads(raw)
        record(support_directory(), **fields)
        return 0
    except Exception:
        return 1  # Never emit user text, field values or exception details.


if __name__ == '__main__':
    sys.exit(main())
