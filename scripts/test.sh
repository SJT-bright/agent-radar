#!/bin/zsh
set -euo pipefail
PROJECT_DIR="${0:A:h:h}"
cd "$PROJECT_DIR"
/usr/bin/python3 -B -m unittest discover -s tests -v
mkdir -p build
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/ContinuationPolicy.swift Sources/TaskCompletionSoundTracker.swift Sources/ReminderSound.swift tests/reminder_sound_checks.swift -o build/reminder-sound-checks
build/reminder-sound-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/SessionNoticeQueue.swift Sources/ControlStyle.swift Sources/FrostedBackdrop.swift Sources/ContinuationToast.swift tests/toast_window_checks.swift -o build/toast-window-checks
build/toast-window-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/SessionNoticeQueue.swift tests/session_notice_queue_checks.swift -o build/session-notice-queue-checks
build/session-notice-queue-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/KeepAwake.swift tests/keep_awake_checks.swift -o build/keep-awake-checks
build/keep-awake-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/PanelHoverState.swift tests/settings_hover_checks.swift -o build/settings-hover-checks
build/settings-hover-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/PanelHoverState.swift Sources/HoverSettingsPopover.swift tests/hover_menu_checks.swift -o build/hover-menu-checks
build/hover-menu-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/NativeMonitor.swift \
  tests/native_monitor_checks.swift -o build/native-monitor-checks
build/native-monitor-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift tests/workspace_conflict_checks.swift -o build/workspace-conflict-checks
build/workspace-conflict-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/QueueInsertionController.swift tests/queue_insertion_checks.swift -o build/queue-insertion-checks
build/queue-insertion-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/session_clock_checks.swift -o build/session-clock-checks
build/session-clock-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/ContinuationPolicy.swift tests/continuation_checks.swift -o build/continuation-checks
build/continuation-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/PromptRules.swift tests/prompt_mode_checks.swift -o build/prompt-mode-checks
build/prompt-mode-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/PromptRules.swift Sources/PromptSettingsDraft.swift tests/prompt_settings_draft_checks.swift -o build/prompt-settings-draft-checks
build/prompt-settings-draft-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/mode_controller_checks.swift -o build/mode-controller-checks
build/mode-controller-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/settings_store_checks.swift -o build/settings-store-checks
build/settings-store-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/monitor_refresh_checks.swift -o build/monitor-refresh-checks
build/monitor-refresh-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift tests/modes_integration/supervision_controller_checks.swift -o build/supervision-controller-checks
build/supervision-controller-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/PromptRules.swift tests/modes_integration/prompt_boundary.swift -o build/prompt-boundary-checks
build/prompt-boundary-checks | /usr/bin/python3 -B tests/modes_integration/prompt_boundary.py
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift tests/modes_integration/request_roundtrip.swift -o build/request-roundtrip-checks
build/request-roundtrip-checks | /usr/bin/python3 -B tests/modes_integration/request_roundtrip.py
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift tests/modes_integration/fast_interruption_checks.swift -o build/fast-interruption-checks
build/fast-interruption-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift tests/modes_integration/recovery_countdown_checks.swift -o build/recovery-countdown-checks
build/recovery-countdown-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PERMISSION_RECOVERY_MODEL_ONLY Sources/PermissionRecovery.swift tests/modes_integration/permission_checks.swift -o build/permission-recovery-checks
build/permission-recovery-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/modes_integration/permission_store_checks.swift -o build/permission-store-checks
build/permission-store-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/modes_integration/supervision_store_checks.swift -o build/supervision-store-checks
build/supervision-store-checks
PYTHONPATH="$PROJECT_DIR/vendor/watchdog:$PROJECT_DIR/build/任务雷达.app/Contents/Resources/python" /usr/bin/python3 -B tests/modes_integration/input_idle_checks.py
/usr/bin/python3 - <<'PY_CLEAN'
from pathlib import Path
Path('build/reminder-sound-checks').unlink(missing_ok=True)
Path('build/toast-window-checks').unlink(missing_ok=True)
Path('build/monitor-refresh-checks').unlink(missing_ok=True)
Path('build/hover-menu-checks').unlink(missing_ok=True)
for name in ('recovery-countdown-checks', 'workspace-conflict-checks', 'session-notice-queue-checks', 'keep-awake-checks', 'settings-hover-checks', 'native-monitor-checks', 'queue-insertion-checks', 'session-clock-checks', 'continuation-checks', 'prompt-mode-checks', 'prompt-settings-draft-checks', 'mode-controller-checks', 'settings-store-checks', 'supervision-controller-checks', 'prompt-boundary-checks', 'request-roundtrip-checks', 'permission-recovery-checks', 'permission-store-checks', 'supervision-store-checks', 'fast-interruption-checks'):
    Path('build', name).unlink(missing_ok=True)
PY_CLEAN
