#!/usr/bin/env python3
"""Build an arm64-only, relocatable macOS app and a drag-to-install DMG.

Uses checksum-pinned archives, hashed Python requirements, and an LGPL-only
FFmpeg built from its published source. Never copies local preferences or .env.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "packaging/runtime.json").read_text())
SPEAKER_ENGINE = json.loads((ROOT / "packaging/speakerkit.json").read_text())
CACHE = ROOT / "build/distribution-deps"
OUTPUT = ROOT / "dist"
CHANNEL = "release"


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def archive(key):
    spec = CONFIG[key]
    target = CACHE / spec["filename"]
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".download")
        with urllib.request.urlopen(spec["url"], timeout=60) as source, temporary.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        temporary.replace(target)
    if hashlib.sha256(target.read_bytes()).hexdigest() != spec["sha256"]:
        raise ValueError("Archive checksum mismatch: " + target.name)
    return target


def unpack(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source) as bundle:
        bundle.extractall(destination, filter="data")


def runtime():
    python = CACHE / "runtime/python"
    if not (python / "bin/python3.12").exists():
        unpack(archive("python"), python.parent)
    else:
        archive("python")  # Recheck the pinned input even when reusing a build cache.
    lock = ROOT / "packaging/requirements.lock"
    fingerprint = hashlib.sha256(lock.read_bytes()).hexdigest()
    stamp = CACHE / "runtime-requirements.sha256"
    if not stamp.exists() or stamp.read_text() != fingerprint:
        run("uv", "pip", "install", "--python", python / "bin/python3.12", "--target", python / "lib/python3.12/site-packages",
            "--python-platform", "aarch64-apple-darwin", "--only-binary", ":all:", "--require-hashes", "--no-deps", "-r", lock,
            env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": CONFIG["minimumMacOS"]})
        stamp.write_text(fingerprint)
    return python


def ffmpeg():
    source = CACHE / ("FFmpeg-n" + CONFIG["ffmpeg"]["version"])
    bundle = archive("ffmpeg")
    if not (source / "configure").exists():
        unpack(bundle, CACHE)
    script = ROOT / "packaging/build-ffmpeg.sh"
    fingerprint = hashlib.sha256(script.read_bytes() + CONFIG["ffmpeg"]["sha256"].encode()).hexdigest()
    stamp = CACHE / "ffmpeg-build.sha256"
    dest = CACHE / "ffmpeg"
    if not (dest / "bin/ffmpeg").exists() or not stamp.exists() or stamp.read_text() != fingerprint:
        run("zsh", script, source, dest)
        stamp.write_text(fingerprint)
    return dest, source


def is_macho(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as file:
        return file.read(4) in [b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"]


def licenses(resources, python, ffmpeg_source):
    destination = resources / "licenses"
    shutil.copytree(ROOT / "docs/licenses", destination, dirs_exist_ok=True)
    shutil.copy2(ffmpeg_source / "COPYING.LGPLv2.1", destination / "FFmpeg-LGPL-2.1.txt")
    build_source = archive("pythonBuildSource")
    with tarfile.open(build_source) as bundle:
        for member in bundle.getmembers():
            if member.isfile() and len(Path(member.name).parts) == 2 and Path(member.name).name.startswith("LICENSE"):
                (destination / ("python-build-standalone-" + Path(member.name).name)).write_bytes(bundle.extractfile(member).read())
    shutil.copy2(python / "lib/python3.12/LICENSE.txt", destination / "CPython-LICENSE.txt")
    # Wheel-provided notices remain in their original locations. Add a useful index.
    script = """import importlib.metadata,json
items=[]
for d in importlib.metadata.distributions():
 files=[str(d.locate_file(f)) for f in d.files or [] if any(x in str(f).lower() for x in ('license','copying','notice')) and d.locate_file(f).is_file()]
 items.append({'name':d.metadata['Name'],'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License','See included files'),'files':files})
print(json.dumps(sorted(items,key=lambda x:x['name'].lower())))
"""
    packages = json.loads(subprocess.check_output([str(python / "bin/python3.12"), "-I", "-B", "-c", script], text=True))
    for item in packages:
        item["files"] = [str(Path(name).relative_to(resources)) for name in item["files"]]
    packages.append({"name": "SpeakerKit / Argmax OSS", "version": SPEAKER_ENGINE["revision"],
                     "license": SPEAKER_ENGINE["license"], "files": ["licenses/argmax-oss-MIT.txt", "licenses/argmax-oss-NOTICES.txt", "licenses/speakerkit-models-NOTICE.txt"],
                     "model": SPEAKER_ENGINE})
    (resources / "dependency-inventory.json").write_text(json.dumps(packages, ensure_ascii=False, indent=2) + "\n")
    body = ["<!doctype html><meta charset='utf-8'><title>VideoDHelper · 第三方许可</title>",
            "<style>body{font:15px -apple-system,sans-serif;max-width:900px;margin:48px auto;line-height:1.7;padding:0 24px}code{word-break:break-word}a{color:#087e80}</style>",
            "<h1>许可证与第三方组件</h1><p>原创代码：<a href='LICENSE'>PolyForm Noncommercial 1.0.0</a>。第三方组件保持各自许可。</p>",
            "<p>FFmpeg 为独立的 LGPL 2.1+ 程序；本项目的非商业限制不适用于它。对应源码与构建脚本随同一 GitHub Release 提供。</p>",
            "<p><a href='https://github.com/ShallM/VideoDHelper/releases'>第三方源码下载</a> · <a href='licenses/FFmpeg-LGPL-2.1.txt'>FFmpeg 许可</a> · <a href='licenses/CPython-LICENSE.txt'>CPython 许可</a></p>"]
    for item in packages:
        body.append(f"<h2>{html.escape(item['name'])} {html.escape(item['version'])}</h2><details><summary>许可信息</summary><pre>{html.escape(item['license'])}</pre></details><ul>")
        for name in item["files"]:
            body.append(f"<li><a href='{html.escape(name, quote=True)}'>{html.escape(name)}</a></li>")
        body.append("</ul>")
    body.append("<h2>CPython 构建依赖许可</h2><ul>")
    for path in sorted(destination.glob("python-build-standalone-*")):
        body.append(f"<li><a href='licenses/{path.name}'>{path.name}</a></li>")
    body.append("</ul>")
    (resources / "LICENSES.html").write_text("\n".join(body))


def build_app():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    python = runtime()
    media, source = ffmpeg()
    run("zsh", ROOT / "scripts/swift-project.sh", "build", "-c", "release")
    app = OUTPUT / "VideoDHelper.app"
    if app.exists():
        shutil.rmtree(app)
    resources = app / "Contents/Resources"
    executable = app / "Contents/MacOS"
    executable.mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(ROOT / ".build/release/VideoDHelper", executable / "VideoDHelper")
    shutil.copytree(python, resources / "python", symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Build utilities are not needed in the runtime, and may have absolute shebangs.
    python_bin = resources / "python/bin"
    for path in python_bin.iterdir():
        if not path.name.startswith("python3") or path.name.endswith("-config"):
            path.unlink()
    for package in (resources / "python/lib/python3.12/site-packages").glob("pip*"):
        if package.is_dir():
            shutil.rmtree(package)
    (resources / "bin").mkdir()
    shutil.copy2(ROOT / ".build/release/recording-diarize", resources / "bin/recording-diarize")
    shutil.copy2(ROOT / "packaging/speakerkit.json", resources / "speakerkit.json")
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy2(media / "bin" / name, resources / "bin" / name)
    (resources / "backend").mkdir()
    for path in (ROOT / "backend").glob("*.py"):
        shutil.copy2(path, resources / "backend" / path.name)
    for name, mode in [("video-worker", "worker"), ("yt-dlp", "yt-dlp"), ("runtime-check", "self-test")]:
        entry = resources / name
        entry.write_text('#!/bin/sh\nset -eu\nRESOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                         'exec "$RESOURCE_DIR/python/bin/python3.12" -I -B "$RESOURCE_DIR/backend/launcher.py" ' + mode + ' "$@"\n')
        entry.chmod(0o755)
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, resources / name)
    licenses(resources, resources / "python", source)
    assets = ROOT / "build/distribution-assets"
    run("swift", ROOT / "packaging/render-assets.swift", assets)
    run("iconutil", "-c", "icns", assets / "AppIcon.iconset", "-o", resources / "AppIcon.icns")
    info = {"CFBundleExecutable": "VideoDHelper", "CFBundleIdentifier": "com.shallm.videodhelper",
            "CFBundleName": "VideoDHelper", "CFBundleDisplayName": "VideoDHelper", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": CONFIG["version"], "CFBundleVersion": CONFIG["build"],
            "LSMinimumSystemVersion": CONFIG["minimumMacOS"], "LSArchitecturePriority": ["arm64"],
            "LSRequiresNativeExecution": True, "NSHighResolutionCapable": True, "CFBundleIconFile": "AppIcon",
            "NSHumanReadableCopyright": "Copyright © 2026 ShallM. PolyForm Noncommercial 1.0.0.",
            "VideoDHelperDistribution": True, "VideoDHelperBuildChannel": CHANNEL,
            "VideoDHelperSourceRevision": revision}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    (resources / "distribution.json").write_text(json.dumps({"version": CONFIG["version"], "architecture": "arm64",
        "minimumMacOS": CONFIG["minimumMacOS"], "notarized": False, "signing": "ad-hoc",
        "sourceRevision": revision, "channel": CHANNEL, "build": CONFIG["build"],
        "runtime": CONFIG, "speakerEngine": SPEAKER_ENGINE}, ensure_ascii=False, indent=2) + "\n")
    # Sign leaves first, then the containing app. No Developer ID is claimed.
    for path in sorted(app.rglob("*")):
        if is_macho(path):
            arches = subprocess.check_output(["lipo", "-archs", str(path)], text=True).split()
            if "arm64" not in arches:
                raise ValueError("Non-arm64 component: " + str(path.relative_to(app)))
            if len(arches) > 1:
                thin = path.with_name(path.name + ".arm64")
                run("lipo", path, "-thin", "arm64", "-output", thin)
                thin.chmod(path.stat().st_mode)
                thin.replace(path)
            # Some upstream wheels and Swift toolchains retain their build
            # machine's search paths even when all required libraries are bundled.
            headers = subprocess.check_output(["otool", "-l", str(path)], text=True)
            for block in headers.split("Load command "):
                if "LC_RPATH" not in block:
                    continue
                match = re.search(r"\bpath\s+(.*?)\s+\(offset", block)
                if match and not match[1].startswith(("@", "/usr/lib", "/System/Library/")):
                    run("install_name_tool", "-delete_rpath", match[1], path, stderr=subprocess.DEVNULL)
            run("codesign", "--force", "--sign", "-", path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("codesign", "--force", "--sign", "-", app)
    run("codesign", "--verify", "--deep", "--strict", app)
    run(resources / "runtime-check")
    return app, assets


def main():
    global OUTPUT, CHANNEL
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, help="Build into a separate directory, e.g. an ignored local preview folder.")
    parser.add_argument("--channel", choices=("release", "local-preview"), default="release")
    args = parser.parse_args()
    CHANNEL = args.channel
    if args.output_dir: OUTPUT = args.output_dir.resolve()
    if os.uname().machine != "arm64":
        raise SystemExit("Build with a native Apple Silicon toolchain.")
    CACHE.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app, assets = build_app()
    if not args.app_only:
        dmg = OUTPUT / f"VideoDHelper-{CONFIG['version']}-macOS-arm64.dmg"
        run("uv", "tool", "run", "--python", "3.12", "--from", "dmgbuild==1.6.7", "dmgbuild", "-s", ROOT / "packaging/dmg-settings.py",
            "-D", "app=" + str(app), "-D", "assets=" + str(assets), "VideoDHelper", dmg)
        run("hdiutil", "verify", dmg)
        # The DMG tool must not change sealed app metadata after signing.
        with tempfile.TemporaryDirectory(prefix="VideoDHelper-image-check-") as mount:
            run("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, dmg)
            try:
                run("codesign", "--verify", "--deep", "--strict", Path(mount) / app.name)
            finally:
                run("hdiutil", "detach", mount)
        source_bundle = OUTPUT / f"VideoDHelper-{CONFIG['version']}-third-party-sources.tar.xz"
        with tarfile.open(source_bundle, "w:xz") as bundle:
            for key in ("ffmpeg", "pythonBuildSource"):
                path = archive(key)
                bundle.add(path, arcname=path.name)
            for name in ("build-ffmpeg.sh", "runtime.json", "speakerkit.json", "requirements.in", "requirements.lock"):
                bundle.add(ROOT / "packaging" / name, arcname="build/" + name)
            bundle.add(ROOT / "THIRD_PARTY_NOTICES.md", arcname="THIRD_PARTY_NOTICES.md")
        manifest = OUTPUT / "build-manifest.json"
        inventory = OUTPUT / "third-party-components.json"
        shutil.copy2(app / "Contents/Resources/distribution.json", manifest)
        shutil.copy2(app / "Contents/Resources/dependency-inventory.json", inventory)
        (OUTPUT / "SHA256SUMS.txt").write_text("".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n" for p in (dmg, source_bundle, manifest, inventory)))
        print("Distribution ready:", dmg)


if __name__ == "__main__":
    main()
