#!/bin/zsh
set -euo pipefail
PROJECT_DIR="${0:A:h:h}"
cd "$PROJECT_DIR"
/usr/bin/python3 -B -m unittest discover -s tests -v
mkdir -p build
/usr/bin/xcrun swiftc -swift-version 5 Sources/KeepAwake.swift tests/keep_awake_checks.swift -o build/keep-awake-checks
build/keep-awake-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/PanelHoverState.swift tests/settings_hover_checks.swift -o build/settings-hover-checks
build/settings-hover-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/NativeMonitor.swift \
  tests/native_monitor_checks.swift -o build/native-monitor-checks
build/native-monitor-checks
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
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift tests/modes_integration/supervision_controller_checks.swift -o build/supervision-controller-checks
build/supervision-controller-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/PromptRules.swift tests/modes_integration/prompt_boundary.swift -o build/prompt-boundary-checks
build/prompt-boundary-checks | /usr/bin/python3 -B tests/modes_integration/prompt_boundary.py
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift tests/modes_integration/request_roundtrip.swift -o build/request-roundtrip-checks
build/request-roundtrip-checks | /usr/bin/python3 -B tests/modes_integration/request_roundtrip.py
/usr/bin/xcrun swiftc -swift-version 5 -D PROMPT_RULES_MODEL_ONLY Sources/Models.swift Sources/PromptRules.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift tests/modes_integration/fast_interruption_checks.swift -o build/fast-interruption-checks
build/fast-interruption-checks
/usr/bin/xcrun swiftc -swift-version 5 -D PERMISSION_RECOVERY_MODEL_ONLY Sources/PermissionRecovery.swift tests/modes_integration/permission_checks.swift -o build/permission-recovery-checks
build/permission-recovery-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/modes_integration/permission_store_checks.swift -o build/permission-store-checks
build/permission-store-checks
/usr/bin/xcrun swiftc -swift-version 5 Sources/Models.swift Sources/RemovedSession.swift Sources/PanelMotion.swift Sources/PanelHoverState.swift Sources/NativeMonitor.swift Sources/QueueInsertionController.swift Sources/PromptRules.swift Sources/PromptSettingsDraft.swift Sources/SupervisionSettingsView.swift Sources/ContinuationPolicy.swift Sources/ContinuationController.swift Sources/KeepAwake.swift Sources/ControlStyle.swift Sources/MonitorStore.swift Sources/PermissionRecovery.swift tests/modes_integration/supervision_store_checks.swift -o build/supervision-store-checks
build/supervision-store-checks
PYTHONPATH="$PROJECT_DIR/vendor/watchdog:$PROJECT_DIR/build/任务雷达.app/Contents/Resources/python" /usr/bin/python3 -B tests/modes_integration/input_idle_checks.py
/usr/bin/python3 - <<'PY_CLEAN'
from pathlib import Path
for name in ('keep-awake-checks', 'settings-hover-checks', 'native-monitor-checks', 'queue-insertion-checks', 'session-clock-checks', 'continuation-checks', 'prompt-mode-checks', 'prompt-settings-draft-checks', 'mode-controller-checks', 'settings-store-checks', 'supervision-controller-checks', 'prompt-boundary-checks', 'request-roundtrip-checks', 'permission-recovery-checks', 'permission-store-checks', 'supervision-store-checks', 'fast-interruption-checks'):
    Path('build', name).unlink(missing_ok=True)
PY_CLEAN
