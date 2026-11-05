"""一次性本地紫卡 OCR worker；仅由显式配置的独立解释器执行。"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_REQUEST_BYTES = 14 * 1024 * 1024


def deny_download(*args, **kwargs):
    raise PermissionError("本地 OCR 禁止自动下载依赖或模型")


def network_guard(event, args):
    # Python 推理依赖的下载、DNS、TCP/UDP 路径均在加载模型之前封锁。
    if event in {
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.sendto",
        "socket.bind",
    }:
        raise PermissionError("本地 OCR worker 禁止访问网络")
    # 不允许模型依赖绕过 Python socket 发起外部下载程序。
    if event in {
        "subprocess.Popen",
        "os.system",
        "os.posix_spawn",
        "os.exec",
        "os.fork",
        "os.forkpty",
    }:
        raise PermissionError("本地 OCR worker 禁止启动其它进程")


def load_module(name, file, package=False):
    spec = importlib.util.spec_from_file_location(
        name, file, submodule_search_locations=[str(file.parent)] if package else None
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("本地 OCR 组件加载失败")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_components(root: Path):
    # 专属命名空间只存在于本次 worker；不执行插件 main 或 core/__init__，不改 sys.path。
    for name, directory in (
        ("_wf_local_plugin", root),
        ("_wf_local_plugin.core", root / "core"),
        ("_wf_local_plugin.core.commands", root / "core/commands"),
    ):
        module = types.ModuleType(name)
        module.__path__ = [str(directory)]
        module.__package__ = name
        sys.modules[name] = module
    manager = load_module("_wf_local_plugin.core.local_ocr", root / "core/local_ocr.py")
    parser = importlib.import_module("_wf_local_plugin.core.local_riven_ocr")
    return manager, parser


def recognize(request):
    if not isinstance(request, dict) or set(request) != {"image", "model_dir"}:
        raise ValueError("本地 OCR 请求格式无效")
    model_dir = Path(request["model_dir"])
    root = Path(__file__).resolve().parent.parent
    if not model_dir.is_absolute() or model_dir.resolve().is_relative_to(root):
        raise ValueError("模型目录需要位于插件源码目录以外的绝对路径")
    try:
        image_bytes = base64.b64decode(request["image"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("图片编码无效") from exc
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("图片不能为空且不能超过 10MiB")

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(io.BytesIO(image_bytes)) as source:
        if source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("图片像素超过本地 OCR 上限")
        orientation = source.getexif().get(274, 1)
        image = ImageOps.exif_transpose(source).convert("RGB")

    manager, parser = load_components(root)
    # 在导入 OCR 前再校验实际模型；检查通过不等于将来文件永远不变。
    paths = manager.checked_models(model_dir)
    from rapidocr import RapidOCR
    from rapidocr.utils.download_file import DownloadFile
    import cv2

    DownloadFile.run = staticmethod(deny_download)
    cv2.setNumThreads(2)
    params = {
        "Global.log_level": "warning",
        "Global.text_score": 0.0,
        "Global.model_root_dir": str(model_dir),
        "Det.model_path": paths["PP-OCRv6_det_small.onnx"],
        "Rec.model_path": paths["PP-OCRv6_rec_small.onnx"],
        "Cls.model_path": paths["ch_ppocr_mobile_v2.0_cls_mobile.onnx"],
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_coreml": False,
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_cann": False,
    }
    ocr = RapidOCR(params=params)
    # 显式确认每个会话只运行 CPU；若上游配置语义变化则失败关闭。
    for part in (ocr.text_det, ocr.text_cls, ocr.text_rec):
        if part is not None and part.session.session.get_providers() != ["CPUExecutionProvider"]:
            raise RuntimeError("本地 OCR 仅允许 CPUExecutionProvider")

    def read(view, *, origin=(0, 0), scale=1.0):
        observed = ocr(view)
        rows = []
        if observed.boxes is not None:
            for index, (box, text, score) in enumerate(
                zip(observed.boxes, observed.txts, observed.scores, strict=True)
            ):
                quad = [[x / scale + origin[0], y / scale + origin[1]] for x, y in box.tolist()]
                rows.append({"id": index, "quad": quad, "text": str(text), "score": float(score)})
        return parser.analyze_raw(
            {
                "width": image.width,
                "height": image.height,
                "orientation": orientation,
                "rows": rows,
            }
        )

    result = read(image)
    result["OCR_calls"] = 1
    region = result.get("candidate", {}).get("region")
    if result.get("recognition_state") == "partial" and region:
        from _wf_local_plugin.core.local_riven_ocr.replay_logic import confirm_local_reads

        box = tuple(round(value) for value in region)
        crop = image.crop(box)
        # 全部卡区使用同一尺寸规则，不按卡名或引擎结果调参数。
        scale = max(1.0, min(3.0, 960 / crop.height, 1600 / crop.width))
        size = (round(crop.width * scale), round(crop.height * scale))
        secondary = read(crop.resize(size, Image.Resampling.LANCZOS), origin=box[:2], scale=scale)
        result = confirm_local_reads(result, secondary)
        result["local_verification"].update(crop_box=list(box), scale=scale)
        result["OCR_calls"] = 2
    return result


def main():
    # 原生库也可能向 fd 1 写日志；协议保留单独 fd，其余日志定向 stderr。
    descriptor = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    with os.fdopen(descriptor, "w", encoding="utf-8") as protocol:
        sys.addaudithook(network_guard)
        try:
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("本地 OCR 请求超限")
            result = recognize(json.loads(raw))
            output = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(output.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("本地 OCR 结果超限")
            protocol.write(output + "\n")
        except Exception as exc:
            protocol.write(
                json.dumps(
                    {"status": "local_error", "message": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                )
                + "\n"
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
