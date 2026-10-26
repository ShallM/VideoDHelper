"""Relocatable entry point for the self-contained macOS distribution."""
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import runpy
import signal
import subprocess
import sys
import threading
import time

RESOURCES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESOURCES / "backend"))


def configure():
    # No inherited developer Python, user site-packages, Homebrew, or shell config.
    tools = RESOURCES / "bin"
    os.environ["PATH"] = str(tools) + ":/usr/bin:/bin:/usr/sbin:/sbin"
    os.environ["VIDEOHELPER_BUNDLED_TOOLS"] = str(tools)
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()


def self_test():
    import ssl
    import torch
    import mlx.core as mx
    import mlx_whisper
    mx.eval(mx.array([1.0, 2.0]) + 1)
    versions = {name: importlib.metadata.version(name) for name in ("yt-dlp", "mlx", "mlx-whisper", "torch")}
    ffmpeg = subprocess.check_output([str(RESOURCES / "bin/ffmpeg"), "-version"], text=True).splitlines()[0]
    ssl.create_default_context()
    result = {"python": platform.python_version(), "arch": platform.machine(), "isolated": bool(sys.flags.isolated),
              "metalAvailable": mx.metal.is_available(), "versions": versions, "ffmpeg": ffmpeg,
              "certificateBundle": Path(os.environ["SSL_CERT_FILE"]).is_file()}
    print(json.dumps(result))
    return 0 if result["arch"] == "arm64" and result["isolated"] and result["metalAvailable"] and result["certificateBundle"] else 1


def main():
    configure()
    mode = sys.argv.pop(1) if len(sys.argv) > 1 else "self-test"
    if mode == "self-test":
        return self_test()
    if mode == "worker":
        import worker
        return worker.main()
    if mode == "yt-dlp":
        os.setpgrp()
        parent = os.getppid()
        def watch_parent():
            while True:
                time.sleep(0.5)
                if parent == 1 or os.getppid() != parent:
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                    return
        threading.Thread(target=watch_parent, daemon=True).start()
        runpy.run_module("yt_dlp", run_name="__main__")
        return 0
    raise ValueError("Unknown bundled tool")


if __name__ == "__main__":
    sys.exit(main())
