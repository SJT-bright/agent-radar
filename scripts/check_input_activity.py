"""Read-only input timing diagnostic. Records durations, never keys or positions."""
import argparse
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = Path('/Applications/任务雷达.app/Contents/Resources')
for path in (RUNTIME / 'python', PROJECT / 'vendor/watchdog'):
    sys.path.insert(0, str(path))
from aiwatch.mac import winops


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, choices=range(1, 21), default=1)
    args = parser.parse_args()
    for index in range(args.samples):
        idle = winops.idle_seconds()
        all_events = winops.Quartz.CGEventSourceSecondsSinceLastEventType(
            winops.Quartz.kCGEventSourceStateHIDSystemState, 0xFFFFFFFF)
        print(json.dumps(dict(sample=index, input_idle_seconds=round(idle, 3),
                              all_events_seconds=round(all_events, 3), user_active=idle < 2)), flush=True)
        if index + 1 < args.samples:
            time.sleep(.5)


if __name__ == '__main__':
    main()
