"""Application-owned, pinned local ASR resources and cross-process leases.

No inference entrypoint downloads files. Installation is an explicit operation;
only a fully verified receipt makes a model usable. External caches are read-only.
"""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import stat
import struct
import time
import urllib.error
import urllib.request
import uuid

from common import UserError, digest, read_json, write_json

CATALOG_PATH = Path(__file__).with_name("asr_catalog.json")
MIN_FREE_BYTES = 256 * 1024 * 1024
_WHISPER_DIMS = {
    "turbo": (128, 51866, 1280, 32, 4),
    "large-v3": (128, 51866, 1280, 32, 32),
    "small": (80, 51865, 768, 12, 12),
}


def catalog():
    value = read_json(CATALOG_PATH)
    if value.get("schemaVersion") != 1 or not isinstance(value.get("models"), list):
        raise UserError("应用的语音模型目录无效，请重新安装应用。")
    return value["models"]


def model_definition(model_id):
    if not isinstance(model_id, str):
        raise UserError("请选择支持的语音转文字模型。")
    definition = next((m for m in catalog() if m["id"] == model_id), None)
    if definition is None or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", model_id):
        raise UserError("请选择支持的语音转文字模型。")
    return definition


def _safe_directory(path, create=False):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise UserError("模型目录不是应用管理的普通文件夹，已停止操作。")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _paths(support, model_id):
    model_definition(model_id)
    root = Path(support).resolve()
    models = _safe_directory(root / "models", True)
    manager = _safe_directory(models / ".manager", True)
    downloads = _safe_directory(models / ".downloads", True)
    activities = _safe_directory(manager / "activities", True)
    return {"root": root, "models": models, "manager": manager,
            "directory": models / model_id, "staging": downloads / model_id,
            "receipt": manager / (model_id + ".receipt.json"),
            "state": manager / (model_id + ".state.json"),
            "lock": manager / (model_id + ".lock"), "compute": manager / "compute.lock",
            "activities": activities}


def _file(base, relative):
    relative = PurePosixPath(relative)
    if relative.is_absolute() or not relative.parts or any(p in ("", ".", "..") for p in relative.parts):
        raise UserError("模型文件清单包含无效路径。")
    current = _safe_directory(base)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UserError("模型文件夹含符号链接，已停止操作。")
    return current


def _component_directory(base, definition, component):
    return _safe_directory(base) if len(definition["components"]) == 1 else _safe_directory(_file(base, component["id"]))


def _regular(path):
    try:
        return not path.is_symlink() and stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _hash(path):
    if not _regular(path):
        raise UserError("模型文件缺失或不是普通文件，请修复下载。")
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _size(directory):
    if directory.is_symlink() or not directory.is_dir():
        return 0
    total = 0
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            path = Path(root) / name
            if _regular(path):
                total += path.stat().st_size
    return total


def _json(path, default=None):
    if not _regular(path):
        return default
    try:
        return read_json(path)
    except (OSError, ValueError, TypeError):
        return default


@contextlib.contextmanager
def _lock(path, exclusive=True, wait=False, on_wait=None):
    if path.is_symlink():
        raise UserError("模型锁文件无效，已停止操作。")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        announced = False
        while True:
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise UserError("该模型正在下载、校验或被任务使用，请完成或暂停当前操作后重试。") from None
                if on_wait and not announced:
                    on_wait(); announced = True
                time.sleep(0.2)
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _activity(paths, model_id, kind, owner=""):
    path = paths["activities"] / f"{model_id}.{uuid.uuid4().hex}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        body = json.dumps({"modelID": model_id, "kind": kind, "owner": owner, "pid": os.getpid()}, ensure_ascii=False).encode()
        os.write(descriptor, body)
        os.fsync(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)
        os.close(descriptor)


def _activities(paths, model_id):
    active = []
    for path in paths["activities"].glob(model_id + ".*.json"):
        if not _regular(path):
            continue
        try:
            with path.open("r") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        value = json.load(stream)
                        if value.get("modelID") == model_id:
                            active.append(value)
                    except (ValueError, TypeError):
                        pass
        except OSError:
            continue
    return active


def _fingerprint(files):
    # Documentation and conversion duration/source paths do not affect model
    # inference. Excluding them lets deletion + reinstall restore the same
    # resource identity instead of needlessly invalidating task checkpoints.
    return digest([{"path": f["path"], "sha256": f.get("identitySHA256", f["sha256"])}
                   for f in files if f["path"] not in ("README.md", "local-conversion.json")])


def _identity_sha(path, definition):
    if definition["engine"] == "mlx-whisper" and path.name == "config.json":
        config = read_json(path)
        dimensions = {k: v for k, v in config.items() if k.startswith("n_")}
        # Older application conversions omit loader-only model_type/dtype
        # defaults. Dimension-equivalent float16 configs describe one model.
        return hashlib.sha256(json.dumps(dimensions or config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return None


def _make_receipt(definition, base, components=None):
    checked = []
    for component in components if components is not None else definition["components"]:
        directory = _component_directory(base, definition, component)
        files = []
        for expected in component["files"]:
            path = _file(directory, expected["path"])
            if not _regular(path) or path.stat().st_size != expected["bytes"] or _hash(path) != expected["sha256"]:
                raise UserError("模型文件未通过完整性校验，请修复下载。")
            info = path.stat()
            item = {**expected, "mtimeNS": info.st_mtime_ns, "ctimeNS": info.st_ctime_ns}
            if (identity_sha := _identity_sha(path, definition)) is not None:
                item["identitySHA256"] = identity_sha
            files.append(item)
        checked.append({**component, "files": files, "fingerprint": _fingerprint(files)})
    snapshot = {"modelID": definition["id"], "engine": definition["engine"], "format": definition["format"],
                "components": [{k: c[k] for k in ("id", "repository", "revision", "fingerprint")} for c in checked]}
    return {"version": 1, "modelID": definition["id"], "components": checked, "snapshot": snapshot}


def _validate_receipt(paths, definition, full=False):
    receipt = _json(paths["receipt"])
    if not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("modelID") != definition["id"]:
        raise UserError("模型尚未完成安装，请在设置中下载。")
    if not isinstance(receipt.get("components"), list) or [c.get("id") for c in receipt["components"]] != [c["id"] for c in definition["components"]]:
        raise UserError("模型组件不完整，请修复下载。")
    try:
        for component in receipt["components"]:
            directory = _component_directory(paths["directory"], definition, component)
            if not component["files"]:
                raise ValueError()
            for item in component["files"]:
                path = _file(directory, item["path"])
                if not _regular(path):
                    raise ValueError()
                info = path.stat()
                if info.st_size != item["bytes"] or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                    raise ValueError()
                if item.get("identitySHA256") != _identity_sha(path, definition):
                    raise ValueError()
                if full:
                    if _hash(path) != item["sha256"]:
                        raise ValueError()
                elif info.st_mtime_ns != item["mtimeNS"] or info.st_ctime_ns != item["ctimeNS"]:
                    raise ValueError()
            if component["fingerprint"] != _fingerprint(component["files"]):
                raise ValueError()
        expected = {"modelID": definition["id"], "engine": definition["engine"], "format": definition["format"],
                    "components": [{k: c[k] for k in ("id", "repository", "revision", "fingerprint")} for c in receipt["components"]]}
        if receipt["snapshot"] != expected:
            raise ValueError()
    except (OSError, KeyError, TypeError, ValueError):
        raise UserError("模型文件已变化或缺失，请在设置中修复下载。") from None
    return receipt


def _converted_components(definition, directory):
    """Recognise previous application conversions without trusting file size."""
    model_id = definition["id"]
    if model_id not in ("turbo", "small"):
        return None
    from local_models import CHECKPOINTS
    conversion = _json(_file(directory, "local-conversion.json"))
    config_path, weight_path = _file(directory, "config.json"), _file(directory, "weights.safetensors")
    config = _json(config_path)
    if (not isinstance(conversion, dict) or conversion.get("sha256") != CHECKPOINTS[model_id][1]
            or conversion.get("format") != "mlx-whisper" or conversion.get("dtype") != "float16"
            or not isinstance(config, dict) or not _regular(weight_path)):
        return None
    if tuple(config.get(k) for k in ("n_mels", "n_vocab", "n_audio_state", "n_audio_layer", "n_text_layer")) != _WHISPER_DIMS[model_id]:
        return None
    # Check that every tensor range is physically present; empty/truncated but
    # plausibly named files must never be adopted as a complete conversion.
    try:
        with weight_path.open("rb") as stream:
            length = struct.unpack("<Q", stream.read(8))[0]
            if not 8 <= length <= 8 * 1024 * 1024:
                return None
            tensors = json.loads(stream.read(length))
        data_size = weight_path.stat().st_size - length - 8
        ranges = []
        for name, tensor in tensors.items():
            if name == "__metadata__":
                continue
            lower, upper = tensor["data_offsets"]
            if not 0 <= lower <= upper <= data_size:
                return None
            ranges.append((lower, upper))
        ranges.sort()
        if len(ranges) < 100 or ranges[0][0] != 0 or ranges[-1][1] != data_size or any(a[1] != b[0] for a, b in zip(ranges, ranges[1:])):
            return None
    except (ValueError, KeyError, TypeError, OSError, struct.error):
        return None
    files = [{"path": name, "bytes": _file(directory, name).stat().st_size, "sha256": _hash(_file(directory, name))}
             for name in ("config.json", "weights.safetensors", "local-conversion.json")]
    canonical = next((f["sha256"] for f in definition["components"][0]["files"] if f["path"] == "weights.safetensors"), None)
    if canonical is not None and files[1]["sha256"] != canonical:
        return None
    source = definition["components"][0]
    return [{"id": "asr", "name": "语音识别", "repository": source["repository"] if canonical else "openai/whisper (local conversion)",
             "revision": source["revision"] if canonical else "official-checkpoint-sha256:" + CHECKPOINTS[model_id][1],
             "origin": "local-conversion", "sourceCheckpointSHA256": CHECKPOINTS[model_id][1], "files": files}]


def _adopt_existing(paths, definition, force=False):
    if (paths["receipt"].exists() and not force) or not paths["directory"].exists():
        return False
    _safe_directory(paths["directory"])
    try:
        receipt = _make_receipt(definition, paths["directory"])
    except UserError:
        # Earlier downloads deliberately excluded documentation. Config and
        # weight hashes still prove those inference resources are complete.
        components = [{**c, "files": [f for f in c["files"] if f["path"] != "README.md"]}
                      for c in definition["components"]]
        try:
            receipt = _make_receipt(definition, paths["directory"], components)
        except UserError:
            components = _converted_components(definition, paths["directory"])
            if components is None:
                return False
            receipt = _make_receipt(definition, paths["directory"], components)
    write_json(paths["receipt"], receipt)
    return True


def _legacy_needed(support, definition, paths):
    if paths["directory"].exists() or paths["staging"].exists() or paths["receipt"].exists():
        return True
    for workspace in ("tasks", "recording-tasks"):
        root = Path(support) / workspace
        if root.is_symlink():
            continue
        for job in root.glob("*/request.json"):
            if job.parent.is_symlink():
                continue
            value = _json(job, {})
            if isinstance(value, dict) and (value.get("asrModelID") or value.get("whisperModel")) == definition["id"]:
                return True
    return False


def inventory(support):
    records = []
    for definition in catalog():
        model_id = definition["id"]
        paths = _paths(support, model_id)
        if definition.get("legacy") and not _legacy_needed(support, definition, paths):
            continue
        active = _activities(paths, model_id)
        if not active and not paths["receipt"].exists() and paths["directory"].exists():
            try:
                with _lock(paths["lock"]):
                    _adopt_existing(paths, definition)
            except (UserError, OSError, ValueError, KeyError, TypeError):
                pass
        state_record = _json(paths["state"], {})
        if not isinstance(state_record, dict):
            state_record = {}
        valid = False
        try:
            _validate_receipt(paths, definition)
            valid = True
        except (UserError, OSError, ValueError, KeyError, TypeError):
            pass
        transferring = any(a.get("kind") == "download" for a in active)
        if transferring:
            state = state_record.get("state", "downloading")
        elif valid:
            state = "ready"
        elif state_record.get("state") in ("failed", "paused"):
            state = state_record["state"]
        elif paths["directory"].exists() or paths["receipt"].exists():
            state = "repair"
        elif paths["staging"].exists():
            state = "paused"
        else:
            state = "uninstalled"
        owners = [a.get("owner") or "本地转录任务" for a in active if a.get("kind") == "inference"]
        messages = {"uninstalled": "尚未下载", "ready": "已安装，可离线使用", "paused": "下载已暂停，可继续",
                    "repair": "文件缺失或已变化，需要修复", "verifying": "正在校验模型文件", "downloading": "正在下载"}
        record = {k: definition[k] for k in ("id", "name", "subtitle", "engine", "format", "license", "legacy")}
        components = [{k: c[k] for k in ("id", "name", "repository", "revision")} |
                      {"bytes": sum(f["bytes"] for f in c["files"])} for c in definition["components"]]
        record.update(downloadBytes=sum(c["bytes"] for c in components), installedBytes=_size(paths["directory"]),
                      partialBytes=_size(paths["staging"]), state=state, path=str(paths["directory"]), components=components,
                      message=state_record.get("message", messages.get(state, "请重试下载")) if state in ("failed", "downloading", "verifying") else messages.get(state, ""),
                      inUseTask="、".join(owners) if owners else None)
        if transferring:
            for key in ("progress", "downloadedBytes", "totalBytes"):
                if key in state_record:
                    record[key] = state_record[key]
        records.append(record)
    return records


@contextlib.contextmanager
def acquire_model(support, model_id, owner="", expected=None):
    definition = model_definition(model_id)
    paths = _paths(support, model_id)
    # Adoption is an explicit registration of an existing application cache,
    # not an installation and never accesses the network.
    if not paths["receipt"].exists() and paths["directory"].exists():
        with _lock(paths["lock"]):
            _adopt_existing(paths, definition)
    with _lock(paths["lock"], exclusive=False), _lock(paths["compute"]), _activity(paths, model_id, "inference", owner):
        receipt = _validate_receipt(paths, definition, full=True)
        if expected is not None and expected != receipt["snapshot"]:
            raise UserError("任务记录的语音模型版本与已安装资源不同，不能混用检查点。请恢复原资源或新建任务。")
        locations = {c["id"]: str(_component_directory(paths["directory"], definition, c)) for c in definition["components"]}
        yield {"snapshot": receipt["snapshot"], "paths": locations}


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _open_download(url, offset):
    headers = {"User-Agent": "VideoDHelper local-model-manager", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30, context=_ssl_context())


def _download_file(component, expected, directory, progress):
    destination = _file(directory, expected["path"])
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _regular(destination) and destination.stat().st_size == expected["bytes"] and _hash(destination) == expected["sha256"]:
        progress(expected["bytes"])
        return
    partial = _file(directory, expected["path"] + ".part")
    if partial.exists() and not _regular(partial):
        raise UserError("未完成模型文件无效，请清理后重试。")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > expected["bytes"]:
        partial.unlink(); offset = 0
    if shutil.disk_usage(directory).free < expected["bytes"] - offset + MIN_FREE_BYTES:
        raise UserError("磁盘剩余空间不足。已保留下载进度，请释放空间后继续。")
    if offset < expected["bytes"]:
        url = f"https://huggingface.co/{component['repository']}/resolve/{component['revision']}/{expected['path']}"
        with _open_download(url, offset) as response:
            status = getattr(response, "status", 200)
            if status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match or int(match[1]) != offset or int(match[3]) != expected["bytes"]:
                    raise UserError("下载服务返回的续传范围无效，已保留进度。")
            elif status == 200:
                offset = 0  # The source did not honour Range; never append it.
            else:
                raise UserError("模型下载服务没有返回有效文件。")
            with partial.open("ab" if offset else "wb") as target:
                try:
                    progress(offset)
                    while True:
                        block = response.read(min(1024 * 1024, expected["bytes"] - offset + 1))
                        if not block:
                            break
                        if offset + len(block) > expected["bytes"]:
                            raise UserError("下载文件长度与固定版本不符，已停止下载。")
                        target.write(block)
                        offset += len(block)
                        progress(offset)
                finally:
                    target.flush(); os.fsync(target.fileno())
    if not partial.exists() or partial.stat().st_size != expected["bytes"]:
        raise UserError("模型文件下载未完成，继续下载会复用已有进度。")
    if _hash(partial) != expected["sha256"]:
        partial.unlink()
        raise UserError("下载文件未通过 SHA-256 校验，已清理该损坏文件，请重试。")
    os.replace(partial, destination)


def _remove_tree(path):
    _safe_directory(path)
    if path.exists():
        shutil.rmtree(path)


def _prune_staging(base, definition, receipt):
    keep = {_file(_component_directory(base, definition, component), item["path"])
            for component in receipt["components"] for item in component["files"]}
    for root, dirs, files in os.walk(base, topdown=False, followlinks=False):
        for name in files:
            path = Path(root) / name
            if path not in keep:
                path.unlink()
        for name in dirs:
            path = Path(root) / name
            if path.is_symlink():
                path.unlink()
            elif not any(path.iterdir()):
                path.rmdir()


def _seed_staging(paths, definition):
    """Reuse intact owned files during a repair, without another network copy."""
    if not paths["directory"].exists():
        return
    for component in definition["components"]:
        old = _component_directory(paths["directory"], definition, component)
        new = _component_directory(paths["staging"], definition, component)
        new.mkdir(parents=True, exist_ok=True, mode=0o700)
        for item in component["files"]:
            source, destination = _file(old, item["path"]), _file(new, item["path"])
            if not _regular(source) or source.stat().st_size != item["bytes"] or _hash(source) != item["sha256"]:
                continue
            if _regular(destination) and destination.stat().st_size == item["bytes"] and _hash(destination) == item["sha256"]:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.unlink(missing_ok=True)
            # Both paths are beneath the same app-owned model root. Linking
            # saves disk space; final receipt stamps are captured after publish.
            os.link(source, destination, follow_symlinks=False)


def _reuse_hub_file(component, item, directory):
    """Copy verified fixed-revision Hub cache files; never own/delete that cache."""
    target = _file(directory, item["path"])
    if target.exists():
        return
    home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface")))
    hub = Path(os.environ.get("HF_HUB_CACHE", str(home / "hub")))
    repository = hub / ("models--" + component["repository"].replace("/", "--"))
    source = repository / "snapshots" / component["revision"] / item["path"]
    try:
        # Hub snapshots may point to blobs inside the same repository cache.
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(repository.resolve()) or not resolved.is_file():
            return
        if resolved.stat().st_size != item["bytes"] or _hash(resolved) != item["sha256"]:
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(resolved, target)
    except (FileNotFoundError, PermissionError):
        return


def download_model(support, model_id, emit=None):
    definition = model_definition(model_id)
    paths = _paths(support, model_id)
    emit = emit or (lambda event: None)
    total = sum(f["bytes"] for c in definition["components"] for f in c["files"])
    last = [0.0]
    def report(state, message, done=None, force=False):
        now = time.monotonic()
        if not force and now - last[0] < 0.25:
            return
        last[0] = now
        value = {"stage": "progress", "modelID": model_id, "state": state, "message": message}
        if done is not None:
            value.update(downloadedBytes=done, totalBytes=total, progress=min(1., done / total) if total else 1.)
        write_json(paths["state"], value)
        emit(value)
    with _lock(paths["lock"]), _activity(paths, model_id, "download"):
        try:
            report("verifying", "检查已有模型文件…", force=True)
            _adopt_existing(paths, definition)
            try:
                receipt = _validate_receipt(paths, definition, full=True)
                # Refresh stat stamps after harmless copies/touches.
                write_json(paths["receipt"], _make_receipt(definition, paths["directory"], receipt["components"]))
                report("ready", "模型已就绪，无需重复下载。", total, True)
                return
            except UserError:
                pass
            if _adopt_existing(paths, definition, force=True):
                report("ready", "已有模型重新校验通过，无需下载。", total, True)
                return
            stage = _safe_directory(paths["staging"], True)
            _seed_staging(paths, definition)
            remaining = max(0, total - _size(stage))
            if shutil.disk_usage(stage).free < remaining + MIN_FREE_BYTES:
                raise UserError("磁盘剩余空间不足。已保留下载进度，请释放空间后继续。")
            # Only an explicit install may convert an external official cache.
            converted = False
            if model_id in ("turbo", "small") and not paths["directory"].exists():
                from local_models import convert_checkpoint, existing_checkpoint
                source = existing_checkpoint(model_id)
                if source is not None:
                    report("verifying", "校验并复用本机已有 Whisper 权重…", force=True)
                    with _lock(paths["compute"], wait=True, on_wait=lambda: report("verifying", "等待本地转录完成，随后转换已有模型…", force=True)):
                        convert_checkpoint(model_id, source, stage, lambda _stage, message, *_: report("verifying", message, force=True))
                    components = _converted_components(definition, stage)
                    if components is None:
                        raise UserError("本机模型转换结果未通过校验，原文件已保留。")
                    receipt = _make_receipt(definition, stage, components)
                    converted = True
            if not converted:
                done = 0
                for component in definition["components"]:
                    directory = _component_directory(stage, definition, component)
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    for item in component["files"]:
                        base = done
                        report("downloading", f"下载 {definition['name']} · {component['name']}", base, True)
                        _reuse_hub_file(component, item, directory)
                        _download_file(component, item, directory,
                                       lambda count, base=base: report("downloading", f"下载 {definition['name']} · {component['name']}", base + count))
                        done += item["bytes"]
                report("verifying", "下载完成，正在校验所有模型组件…", total, True)
                receipt = _make_receipt(definition, stage)
            # The old directory has already failed verification. Keep it until
            # the replacement is complete, then publish the verified tree.
            _prune_staging(stage, definition, receipt)
            _remove_tree(paths["directory"])
            os.replace(stage, paths["directory"])
            # Removing old hardlinks can change file ctime. Revalidate and
            # capture final stamps only after publishing the complete tree.
            write_json(paths["receipt"], _make_receipt(definition, paths["directory"], receipt["components"]))
            report("ready", "模型已安装，可离线使用。", total, True)
        except (KeyboardInterrupt, SystemExit):
            report("paused", "下载已暂停，已保存可继续的进度。", force=True)
            raise
        except BaseException as error:
            if isinstance(error, UserError):
                message = str(error)
            elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
                message = "磁盘空间已用尽，已保留下载进度。请释放空间后继续。"
            elif isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM):
                message = "无法写入应用模型目录，请检查文件权限后继续；已有模型和下载进度保留。"
            elif isinstance(error, urllib.error.HTTPError):
                message = f"模型下载来源暂不可访问（HTTP {error.code}），请检查网络或稍后继续；已保留下载进度。"
            elif isinstance(error, (TimeoutError, socket.timeout)) or isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout)):
                message = "连接 Hugging Face 超时，已保留下载进度；请检查网络后继续。"
            elif isinstance(getattr(error, "reason", error), ssl.SSLError):
                message = "模型下载的 HTTPS 证书验证失败，已停止连接并保留进度；请检查网络或系统时间。"
            elif isinstance(error, urllib.error.URLError):
                message = "无法连接 Hugging Face，已保留下载进度；请检查网络或代理后继续。"
            else:
                message = "模型下载未完成，请检查 Hugging Face 连接后继续；已保留下载进度。"
            report("failed", message, force=True)
            if isinstance(error, UserError):
                raise
            raise UserError(message) from None


def remove_model(support, model_id, partial_only=False):
    paths = _paths(support, model_id)
    with _lock(paths["lock"]):
        _remove_tree(paths["staging"])
        if not partial_only:
            _remove_tree(paths["directory"])
            paths["receipt"].unlink(missing_ok=True)
        paths["state"].unlink(missing_ok=True)


def run_cli(action, model_id, support):
    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    try:
        if action == "list":
            pass
        elif action == "download":
            download_model(support, model_id, emit)
        elif action in ("remove", "clear"):
            remove_model(support, model_id, partial_only=action == "clear")
        else:
            raise UserError("不支持的模型管理操作。")
        emit({"stage": "models", "models": inventory(support)})
        return 0
    except (KeyboardInterrupt, SystemExit):
        emit({"stage": "paused", "modelID": model_id, "message": "下载已暂停，已保存进度。"})
        return 0
    except (UserError, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, UserError) else "模型资源无法读取或操作，请检查目录和磁盘空间。"
        emit({"stage": "error", "modelID": model_id, "message": message})
        return 2
