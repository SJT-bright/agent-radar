"""Exercise the real input-idle implementation with deterministic native responses."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock, patch
from aiwatch.mac import inject, winops
count = 0
def check(value):
    global count
    assert value
    count += 1
function = Mock(return_value=5.5)
library = SimpleNamespace(CGEventSourceSecondsSinceLastEventType=function)
with patch.object(winops, '_CG', None), patch.object(winops, '_CG_TRIED', False), patch.object(winops.ctypes, 'CDLL', return_value=library):
    check(winops.idle_seconds() == 5.5)
    check(function.argtypes == [ctypes.c_int32, ctypes.c_uint32])
    check([call.args[1] for call in function.call_args_list] == list(winops._INPUT_EVENT_TYPES))
    check(all(call.args[0] == int(winops.Quartz.kCGEventSourceStateHIDSystemState)
              for call in function.call_args_list))
    check(0xFFFFFFFF not in [call.args[1] for call in function.call_args_list])
# Launching Python/AppKit can reset "any input" without a human event. It must
# neither prevent starting navigation nor make a later guard cancel that work.
clocks = {kind: 8.0 for kind in winops._INPUT_EVENT_TYPES}
clocks.update({0: 0, 29: 0, 32: 0, 0xFFFFFFFF: 0})
with patch.object(winops, '_coregraphics', return_value=SimpleNamespace(
        CGEventSourceSecondsSinceLastEventType=lambda _, kind: clocks[kind])):
    check(winops.idle_seconds() == 8 and not winops.user_active(2))
    for kind in winops._INPUT_EVENT_TYPES:
        clocks[kind] = .1
        check(winops.user_active(2))
        clocks[kind] = 8.0
with patch.object(winops, '_coregraphics', return_value=None):
    check(winops.idle_seconds() == 0 and winops.user_active(2))
for value in (float('nan'), float('inf'), -1):
    with patch.object(winops, '_coregraphics', return_value=SimpleNamespace(CGEventSourceSecondsSinceLastEventType=lambda *_: value)):
        check(winops.idle_seconds() == 0 and winops.user_active(2))
with patch.object(winops, '_coregraphics', side_effect=RuntimeError('unavailable')):
    check(winops.idle_seconds() == 0)
for value, expected in ((0, True), (1.999, True), (2, False), (2.001, False)):
    with patch.object(winops, 'idle_seconds', return_value=value):
        check(winops.user_active(2) == expected)
# Exercise the injector with the idle guard: HID posting of a private event
# resets the hardware clock; session posting must leave it alone. A real event
# that arrives during the same operation must still stop the guard.
clocks = {kind: 8.0 for kind in winops._INPUT_EVENT_TYPES}
posts = []
def native_post(tap, event):
    posts.append((tap, event))
    if tap == 0:
        clocks[event] = 0.0
fake_quartz = SimpleNamespace(kCGHIDEventTap=0, kCGSessionEventTap=1,
                             CGEventPost=native_post)
with patch.object(inject, 'Quartz', fake_quartz), patch.object(winops, '_coregraphics', return_value=SimpleNamespace(
        CGEventSourceSecondsSinceLastEventType=lambda _, kind: clocks[kind])):
    for kind in (5, 1, 2, 10, 11, 12):
        inject.post(kind)
        check(not winops.user_active(2))
    check(all(tap == 1 for tap, _ in posts))
    clocks[10] = .1
    check(winops.user_active(2))
print(f'Hardware idle: {count} checks passed')
