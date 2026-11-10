#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[[ "$(uname -s)" == Darwin ]] || { echo 'Run this script on macOS.' >&2; exit 1; }
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"
BUILD_DIR="${MACOS_BUILD_DIR:-build-macos}"
BACKEND_BINARY="$BUILD_DIR/light-agent"
if [[ ! -x "$BACKEND_BINARY" ]]; then
  cmake -S . -B "$BUILD_DIR"
  cmake --build "$BUILD_DIR" --target light-agent -j "${JOBS:-3}"
fi
APP="dist/light-agent.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
python3 tools/gen_icon.py
cp "$BACKEND_BINARY" "$APP/Contents/MacOS/light-agent"
cp dist/LightAgent.icns "$APP/Contents/Resources/LightAgent.icns"
cp ../LICENSE ../NOTICE "$APP/Contents/Resources/"
xcrun clang -fobjc-arc -Wall -Wextra -framework Cocoa -framework WebKit \
  tools/desktop_macos.m -o "$APP/Contents/MacOS/LightAgent"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>org.cognitiveos.desktop</string>
<key>CFBundleName</key><string>light-agent</string>
<key>CFBundleDisplayName</key><string>light-agent</string>
<key>CFBundleExecutable</key><string>LightAgent</string>
<key>CFBundleIconFile</key><string>LightAgent.icns</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>3.1.1</string>
<key>CFBundleVersion</key><string>1001</string>
<key>LSMinimumSystemVersion</key><string>12.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
plutil -lint "$APP/Contents/Info.plist"
IDENTITY="${MACOS_SIGN_IDENTITY:--}"
codesign --force --options runtime --sign "$IDENTITY" "$APP/Contents/MacOS/light-agent"
codesign --force --options runtime --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict "$APP"
mkdir -p dist
ARCH=$(uname -m)
if [[ "$ARCH" == x86_64 ]]; then ARCH=x64; fi
ditto -c -k --sequesterRsrc --keepParent "$APP" "dist/light-agent-macos-$ARCH.zip"
# Standard macOS Installer package; no shell commands or manual copying required.
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")
PKG="dist/light-agent-macos-$ARCH.pkg"
pkgbuild --component "$APP" --install-location /Applications \
  --identifier org.cognitiveos.desktop --version "$VERSION" "$PKG"
if [[ -n "${MACOS_INSTALLER_IDENTITY:-}" ]]; then
  productsign --sign "$MACOS_INSTALLER_IDENTITY" "$PKG" "$PKG.signed"
  mv "$PKG.signed" "$PKG"
  pkgutil --check-signature "$PKG"
fi
# Inspect the actual package payload and destination on both macOS CI runners.
CHECK_DIR=$(mktemp -d)
trap 'rm -rf "$CHECK_DIR"' EXIT
pkgutil --expand "$PKG" "$CHECK_DIR/expanded"
python3 tools/verify_macos_pkg.py "$CHECK_DIR/expanded" "$VERSION"
echo "Built $APP, ZIP and Installer PKG (app identity: $IDENTITY)."
echo 'Public distribution additionally requires Developer ID signing and notarization.'
