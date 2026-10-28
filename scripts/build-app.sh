#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
cd "$PROJECT_ROOT"
if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    print -u2 'Missing project Python environment. Run: zsh scripts/setup-tools.sh'
    exit 1
fi
zsh "$PROJECT_ROOT/scripts/swift-project.sh" build -c release
APP_PATH="$PROJECT_ROOT/build/VideoDHelper.app"
mkdir -p "$APP_PATH/Contents/MacOS"
cp "$PROJECT_ROOT/.build/release/VideoDHelper" "$APP_PATH/Contents/MacOS/VideoDHelper"
mkdir -p "$APP_PATH/Contents/Resources"
cp "$PROJECT_ROOT/.build/release/recording-diarize" "$APP_PATH/Contents/Resources/recording-diarize"
codesign --force --sign - "$APP_PATH/Contents/Resources/recording-diarize"
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    mkdir -p "$APP_PATH/Contents/Resources"
    printf '#!/bin/zsh\nexec %q -m yt_dlp "$@"\n' "$PROJECT_ROOT/.venv/bin/python" > "$APP_PATH/Contents/Resources/yt-dlp"
    chmod +x "$APP_PATH/Contents/Resources/yt-dlp"
    mkdir -p "$APP_PATH/Contents/Resources/backend"
    cp "$PROJECT_ROOT"/backend/*.py "$APP_PATH/Contents/Resources/backend/"
    printf '#!/bin/zsh\nexport PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"\nexec %q -u "${0:A:h}/backend/worker.py" "$@"\n' "$PROJECT_ROOT/.venv/bin/python" > "$APP_PATH/Contents/Resources/video-worker"
    chmod +x "$APP_PATH/Contents/Resources/video-worker"
    if [[ -f "$PROJECT_ROOT/.env" ]]; then
        printf '%s\n' "$PROJECT_ROOT/.env" > "$APP_PATH/Contents/Resources/development-env-path.txt"
    else
        rm -f "$APP_PATH/Contents/Resources/development-env-path.txt"
    fi
fi
mkdir -p "$APP_PATH/Contents/Resources/licenses"
chmod -R u+w "$APP_PATH/Contents/Resources/licenses"
cp "$PROJECT_ROOT/LICENSE" "$PROJECT_ROOT/NOTICE" "$PROJECT_ROOT/THIRD_PARTY_NOTICES.md" "$APP_PATH/Contents/Resources/"
cp "$PROJECT_ROOT"/docs/licenses/*.txt "$APP_PATH/Contents/Resources/licenses/"
cp "$PROJECT_ROOT/packaging/speakerkit.json" "$APP_PATH/Contents/Resources/speakerkit.json"
cat > "$APP_PATH/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>VideoDHelper</string>
<key>CFBundleIdentifier</key><string>local.videodhelper.app</string>
<key>CFBundleName</key><string>VideoDHelper</string>
<key>CFBundleDisplayName</key><string>VideoDHelper</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.7.0</string>
<key>CFBundleVersion</key><string>14</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$APP_PATH"
printf 'Built app: %s\n' "$APP_PATH"
