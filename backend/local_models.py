"""Reuse official OpenAI checkpoints locally; inference remains MLX/Metal.

Tensor naming/layout follows Apple's MLX Whisper conversion format:
https://github.com/ml-explore/mlx-examples/blob/main/whisper/convert.py
This converter accepts only the two known, SHA-256 verified checkpoints.
"""
import hashlib
import os
from pathlib import Path
import tempfile
import time

from common import UserError, write_json

CHECKPOINTS = {
    "turbo": ("large-v3-turbo.pt", "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a",
              b"ABzY8j^C+e0{>%RARaKHP%t(lGR*)0g!tONPyhe`"),
    "small": ("small.pt", "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794",
              b"ABzY8DmU6=0{>%Rpa?J`kvJ6qF(V^F86#Xh7JUGMK}P<N0000"),
}


def existing_checkpoint(name):
    candidate = Path.home() / ".cache" / "whisper" / CHECKPOINTS[name][0]
    return candidate if candidate.is_file() else None


def ready(directory):
    return (directory / "config.json").is_file() and any(
        p.is_file() and p.stat().st_size > 1_000_000 for p in [directory / "weights.safetensors", directory / "weights.npz"])


def convert_checkpoint(name, source, directory, emit):
    start = time.monotonic()
    expected = CHECKPOINTS[name][1]
    emit("model", "发现本机 Whisper 权重，正在校验；无需重新下载…")
    sha = hashlib.sha256()
    with source.open("rb") as file:
        for block in iter(lambda: file.read(8 * 1024 * 1024), b""):
            sha.update(block)
    if sha.hexdigest() != expected:
        raise UserError("本机 Whisper 权重校验不匹配，已保留原文件。请检查模型是否完整。")
    emit("model", "校验通过，正在本机转换为 MLX 格式（仅首次需要）…")
    import torch
    import mlx.core as mx
    from mlx.utils import tree_flatten
    from mlx_whisper.whisper import ModelDimensions, Whisper
    if not mx.metal.is_available():
        raise UserError("Metal GPU 不可用，已停止转录，没有自动退回纯 CPU。")
    checkpoint = torch.load(str(source), map_location="cpu", weights_only=True)
    config = checkpoint["dims"]
    model = Whisper(ModelDimensions(**config), mx.float16)
    weights = []
    for key, tensor in checkpoint["model_state_dict"].items():
        if key == "encoder.positional_embedding":
            continue
        target = key.replace(".mlp.0.", ".mlp1.").replace(".mlp.2.", ".mlp2.")
        array = tensor.detach().numpy()
        if tensor.ndim == 3 and ".conv" in key:
            array = array.transpose(0, 2, 1)
        weights.append((target, mx.array(array).astype(mx.float16)))
    supplied = {k for k, _ in weights}
    required = {k for k, _ in tree_flatten(model.parameters())} - {"alignment_heads"}
    if supplied != required:
        raise UserError("本机权重与 MLX 模型结构不匹配，未写入转换结果。")
    model.load_weights(weights, strict=False)
    model.set_alignment_heads(CHECKPOINTS[name][2])
    mx.eval(model.parameters())
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(prefix=".converting-", suffix=".safetensors", dir=directory)
    os.close(fd)
    try:
        mx.save_safetensors(path, dict(tree_flatten(model.parameters())))
        os.replace(path, directory / "weights.safetensors")
        write_json(directory / "config.json", config)
        write_json(directory / "local-conversion.json", {
            "source": str(source), "sha256": expected, "dtype": "float16", "format": "mlx-whisper",
            "seconds": round(time.monotonic() - start, 2), "inferenceDevice": str(mx.default_device()),
        })
    finally:
        Path(path).unlink(missing_ok=True)
    emit("model", "已复用本机模型；MLX Metal GPU 就绪。")
    return str(directory)
