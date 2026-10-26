#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
cd "$PROJECT_ROOT"
mkdir -p .build

# Some CLT upgrades leave an obsolete private PackageDescription interface next
# to the current public interface and dylib. Redirect that one import using a
# project-local VFS overlay; never modify the installed toolchain.
OVERLAY_PATH="$(python3 - <<'PY'
import json, platform, subprocess
from pathlib import Path
compiler = Path(subprocess.check_output(['xcrun', '--find', 'swiftc'], text=True).strip())
module = compiler.parent.parent / 'lib/swift/pm/ManifestAPI/PackageDescription.swiftmodule'
stem = platform.machine() + '-apple-macos'
public = module / (stem + '.swiftinterface')
private = module / (stem + '.private.swiftinterface')
def version(path):
    return next((line for line in path.read_text().splitlines()[:6] if line.startswith('// swift-compiler-version:')), '')
if public.exists() and private.exists() and version(public) != version(private):
    overlay = Path('.build/manifest-overlay.json').resolve()
    overlay.write_text(json.dumps({'version': 0, 'case-sensitive': False, 'roots': [
        {'type': 'file', 'name': str(private), 'external-contents': str(public)}
    ]}))
    print(overlay)
PY
)"

export SWIFTPM_MODULECACHE_OVERRIDE="$PROJECT_ROOT/.build/current-module-cache"
SWIFT_ARGUMENTS=("$@")
DEVELOPER_DIRECTORY="$(xcode-select -p)"
TEST_FRAMEWORKS="$DEVELOPER_DIRECTORY/Library/Developer/Frameworks"
TEST_LIBRARIES="$DEVELOPER_DIRECTORY/Library/Developer/usr/lib"
# CLT contains Swift Testing but SwiftPM does not discover its framework or
# interop-library paths automatically on this installation.
if [[ "${1:-}" == "test" && -d "$TEST_FRAMEWORKS/Testing.framework" ]]; then
    SWIFT_ARGUMENTS+=(--disable-xctest
        -Xswiftc -F -Xswiftc "$TEST_FRAMEWORKS"
        -Xlinker -F -Xlinker "$TEST_FRAMEWORKS"
        -Xlinker -rpath -Xlinker "$TEST_FRAMEWORKS"
        -Xlinker -rpath -Xlinker "$TEST_LIBRARIES")
fi
if [[ -n "$OVERLAY_PATH" ]]; then
    exec swift "${SWIFT_ARGUMENTS[@]}" --manifest-cache none -Xbuild-tools-swiftc -vfsoverlay -Xbuild-tools-swiftc "$OVERLAY_PATH"
else
    exec swift "${SWIFT_ARGUMENTS[@]}"
fi
