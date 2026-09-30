#!/bin/zsh
set -euo pipefail
PROJECT_DIR="${0:A:h:h}"
APP_DIR="$PROJECT_DIR/build/任务雷达.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/collector" "$PROJECT_DIR/build/module-cache"
/usr/bin/plutil -lint "$PROJECT_DIR/scripts/Info.plist"
/usr/bin/xcrun swiftc -swift-version 5 -O -module-cache-path "$PROJECT_DIR/build/module-cache" \
  -target arm64-apple-macosx13.0 -framework AppKit -framework SwiftUI -framework ApplicationServices -framework ServiceManagement \
  "$PROJECT_DIR"/Sources/*.swift -o "$APP_DIR/Contents/MacOS/AgentRadar"
cp "$PROJECT_DIR/scripts/Info.plist" "$APP_DIR/Contents/Info.plist"
cp "$PROJECT_DIR/Assets/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
cp "$PROJECT_DIR"/collector/*.py "$APP_DIR/Contents/Resources/collector/"
/usr/bin/python3 "$PROJECT_DIR/scripts/bundle_watchdog.py" "$APP_DIR/Contents/Resources"
mkdir -p "$APP_DIR/Contents/Resources/icons"
cp "$PROJECT_DIR"/Assets/icons/*.png "$APP_DIR/Contents/Resources/icons/"

# Only use an already-valid identity. Building never imports or trusts certificates.
SIGN_IDENTITY="-"
IDENTITY="AgentRadar Local"
if /usr/bin/security find-identity -p codesigning -v 2>/dev/null | /usr/bin/grep -q "\"$IDENTITY\""; then
  SIGN_IDENTITY="$IDENTITY"
elif [[ "${AGENT_RADAR_ALLOW_ADHOC:-0}" == "1" ]]; then
  echo "显式启用开发用 ad-hoc 签名；不要覆盖证书签名的正式安装版，辅助功能授权需重新核验。"
else
  echo "构建停止：未找到已有有效 AgentRadar Local 签名身份。为保留正式版代码身份，不自动回退 ad-hoc，也不创建或信任新证书。" >&2
  echo "仅开发测试可显式设置 AGENT_RADAR_ALLOW_ADHOC=1。" >&2
  exit 1
fi
/usr/bin/codesign --force --deep --sign "$SIGN_IDENTITY" --identifier local.agentradar.desktop "$APP_DIR"
printf '%s\n' "$APP_DIR"
