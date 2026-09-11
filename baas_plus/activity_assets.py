"""活动截图资源库：当前服模板缺失时的本地补齐与自校验

背景（实测结论）：BAAS 的活动截图资源按服存放
（`src/images/<identifier>/activity/<模块>/enter1|2|3.png` +
`src/images/<identifier>/x_y_range/activity/<模块>.py`），上游常常只收录了部分服的
资源。缺资源时 BAAS 的 `position.init_image_data` 会 importlib 当前服 x_y_range
失败 → 「Failed to initialize image data」→ 整场执行的模板匹配全废，因此
BAAS-Plus 不允许把这种模块交给 BAAS（见 baas_bridge.list_activity_modules）。

本模块把 BAAS-Plus 自己准备的模板存到 `data/activity_patches/<identifier>/<模块>/`，
运行时由 baas_bridge 注入 BAAS 的 `core.position.image_dic` /
`image_x_y_range`（内存注入，不修改 BAAS 源码）。

两条硬约束（决定了像素来源只能是"现场截图"）：

1. **坐标框可以沿用同活动其它服的 x_y_range**（上游 Global PR #576 也是这么做的：
   三语各拍 PNG、xyrange 直接沿用日服），但 PNG 像素必须来自当前服画面。
   BAAS 里 enter1 的用法是 `image.compare_image`：按 x_y_range 框裁一块做 1:1
   比对（先 rgb 均值预筛、再单点 matchTemplate，阈值 0.8），不搜索、无宽容度。
   实测：国服 2026-09 水仗活动借日服 2026 复刻版模板 → 1:1 仅 0.534（滑窗 0.855，
   只能骗过 BAAS-Plus 自己的轮播图门禁）；借日服**同版本**皮肤则 0.959。
2. **裁 enter1 时必须确认轮播图当前页就是目标活动**：enter1 实质是"当前页皮肤
   指纹"（该区域是轮播图美术，不是按钮）。所以本模块只做"按框裁剪 + 自校验"，
   不猜测来源、不做跨服搬运。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

# 活动模块的截图键（与 BAAS x_y_range 的键名一致）
ASSET_KEYS: tuple[str, ...] = ("enter1", "enter2", "enter3")
# 1280x720 基准：BAAS 的 x_y_range 坐标与模板都是 1280x720 下的值
BASE_WIDTH = 1280
# 裁剪结果的自校验阈值（与 BAAS compare_image 默认阈值一致）
BOX_THRESHOLD = 0.8
# rgb 均值预筛容差（BAAS compare_image_rgb 的 rgb_diff 默认值）
RGB_DIFF = 20
# 低于该面积的模板不参与"滑窗判定"（TM_CCOEFF_NORMED 在小模板上分数会虚高：
# 实测 20x10/22x20 的模板在完全无关的轮播图上也能刷到 0.80~0.91）
MIN_TRUSTED_TEMPLATE_AREA = 1500
# 小模板只能用极高的滑窗分兜底（精确同图 ≈1.0，实测假阳性最高 0.913）
SMALL_TEMPLATE_MIN_SCORE = 0.95
# 模块名/服标识做路径拼接前必须校验（BAAS 模块名含撇号，如 Tr..."'s"）
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_'\-]*$")


def is_safe_name(name: str) -> bool:
    """模块名/标识是否可以安全用作路径片段（防目录穿越）"""
    return bool(name) and bool(_SAFE_NAME_RE.match(name)) and ".." not in name


def patch_root(data_dir: str | Path) -> Path:
    """资源库根目录：<data_dir>/activity_patches"""
    return Path(data_dir) / "activity_patches"


def module_dir(data_dir: str | Path, identifier: str, module: str) -> Path:
    """某个模块的资源目录（同时做名字校验）"""
    if not is_safe_name(identifier) or not is_safe_name(module):
        raise ValueError(f"非法标识: identifier={identifier!r} module={module!r}")
    return patch_root(data_dir) / identifier / module


def meta_path(data_dir: str | Path, identifier: str, module: str) -> Path:
    return module_dir(data_dir, identifier, module) / "meta.json"


def asset_path(data_dir: str | Path, identifier: str, module: str, name: str) -> Path:
    return module_dir(data_dir, identifier, module) / f"{name}.png"


def load_meta(data_dir: str | Path, identifier: str, module: str) -> dict[str, Any] | None:
    """读取资源库元信息（不存在/损坏返回 None）"""
    try:
        path = meta_path(data_dir, identifier, module)
    except ValueError:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def present_assets(data_dir: str | Path, identifier: str, module: str) -> list[str]:
    """资源库里实际存在的模板键（按 ASSET_KEYS 顺序）"""
    try:
        d = module_dir(data_dir, identifier, module)
    except ValueError:
        return []
    return [k for k in ASSET_KEYS if (d / f"{k}.png").is_file()]


def read_asset(data_dir: str | Path, identifier: str, module: str, name: str) -> bytes | None:
    """读取模板 PNG 原始字节（供内存注入）"""
    try:
        path = asset_path(data_dir, identifier, module, name)
    except ValueError:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---- 坐标框（来自 BAAS 的 x_y_range 文件，纯文本解析，不 import）----

_BOXES_RE = re.compile(r"x_y_range\s*=\s*(\{.*?\})", re.S)


def boxes_from_xyrange_file(path: str | Path) -> dict[str, list[int]]:
    """从 BAAS 的 `x_y_range/activity/<模块>.py` 解析坐标框

    用 ast.literal_eval 解析字典字面量（不 import 该模块，避免副作用）。
    解析失败返回空字典。
    """
    import ast

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    m = _BOXES_RE.search(text)
    if not m:
        return {}
    try:
        data = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return {}
    boxes: dict[str, list[int]] = {}
    if isinstance(data, dict):
        for key in ASSET_KEYS:
            box = data.get(key)
            if isinstance(box, (list, tuple)) and len(box) == 4:
                try:
                    boxes[key] = [int(v) for v in box]
                except (TypeError, ValueError):
                    continue
    return boxes


# ---- 图像裁剪与打分（cv2 惰性导入：BAAS-Plus 自身不依赖 opencv）----


def _cv2() -> Any:
    try:
        import cv2  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "活动资源裁剪/校验需要 opencv（cv2）。BAAS 运行环境自带 opencv-python；"
            "若在独立环境使用该功能，请安装 opencv-python-headless。"
        ) from exc
    return cv2


def decode_frame(data: bytes) -> Any:
    """把上传的图片字节解码成 BGR ndarray（失败抛 ValueError）"""
    import numpy as np

    cv2 = _cv2()
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        raise ValueError("无法解码图片（仅支持 PNG/JPG 等常见格式）")
    return img


def frame_ratio(frame: Any) -> float:
    """截图的 1280x720 缩放比（BAAS ratio 同义：宽 / 1280）"""
    width = int(frame.shape[1])
    return width / BASE_WIDTH if width else 1.0


def scale_box(box: Iterable[int], ratio: float) -> tuple[int, int, int, int]:
    """1280x720 基准坐标 → 实际截图坐标"""
    x1, y1, x2, y2 = (int(v) for v in box)
    return (
        int(round(x1 * ratio)),
        int(round(y1 * ratio)),
        int(round(x2 * ratio)),
        int(round(y2 * ratio)),
    )


def _clip_box(box: tuple[int, int, int, int], frame: Any) -> tuple[int, int, int, int]:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
    y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
    return x1, y1, x2, y2


def crop_box(frame: Any, box: Iterable[int], ratio: float | None = None) -> Any:
    """按 1280x720 基准框裁剪截图（自动按 ratio 缩放并夹到画面内）"""
    r = frame_ratio(frame) if ratio is None else ratio
    x1, y1, x2, y2 = _clip_box(scale_box(box, r), frame)
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise ValueError(f"裁剪区域过小（box={list(box)}，ratio={r:.2f}）")
    return frame[y1:y2, x1:x2].copy()


def score_in_box(frame: Any, box: Iterable[int], template: Any, ratio: float | None = None) -> dict[str, Any]:
    """BAAS `compare_image` 口径：按框裁一块做 1:1 比对

    返回 {score, rgb_ok, mean_template, mean_frame}；rgb 预筛不过时 score 为 None
    （BAAS 会直接判 False）。
    """
    import numpy as np

    cv2 = _cv2()
    patch = crop_box(frame, box, ratio)
    m_t = [float(v) for v in np.mean(template, axis=(0, 1))]
    m_f = [float(v) for v in np.mean(patch, axis=(0, 1))]
    rgb_ok = all(abs(m_t[i] - m_f[i]) <= RGB_DIFF for i in range(3))
    score = None
    if rgb_ok:
        resized = cv2.resize(patch, (template.shape[1], template.shape[0]), interpolation=cv2.INTER_AREA)
        score = float(cv2.matchTemplate(resized, template, cv2.TM_CCOEFF_NORMED)[0][0])
    return {
        "score": None if score is None else round(score, 4),
        "rgb_ok": rgb_ok,
        "mean_template": [round(v, 1) for v in m_t],
        "mean_frame": [round(v, 1) for v in m_f],
    }


def score_slide(region: Any, template: Any) -> float:
    """滑窗口径：在给定区域内搜索模板（BAAS-Plus 自己的轮播图门禁算法）"""
    cv2 = _cv2()
    if template.shape[0] > region.shape[0] or template.shape[1] > region.shape[1]:
        return 0.0
    res = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    return round(float(cv2.minMaxLoc(res)[1]), 4)


def template_area(template: Any) -> int:
    return int(template.shape[0]) * int(template.shape[1])


def _looks_blank(image: Any) -> bool:
    """裁剪结果是否近似纯色（截图取错区域/全黑时的兜底检查）"""
    import numpy as np

    cv2 = _cv2()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return float(np.std(gray)) < 3.0


def store_frame_assets(
    data_dir: str | Path,
    identifier: str,
    module: str,
    frame: Any,
    boxes: dict[str, list[int]],
    keys: Iterable[str],
    source: str,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从现场截图裁剪并保存模板（已存在的模板默认不覆盖，除非 keys 指定）

    返回 {"saved": [...], "skipped": [...], "verification": {...}}
    """
    cv2 = _cv2()
    d = module_dir(data_dir, identifier, module)  # 顺带校验名字
    d.mkdir(parents=True, exist_ok=True)
    ratio = frame_ratio(frame)
    saved: list[str] = []
    skipped: list[str] = []
    verification: dict[str, Any] = {}
    for key in keys:
        box = boxes.get(key)
        if not box:
            skipped.append(key)
            continue
        patch = crop_box(frame, box, ratio)
        if _looks_blank(patch):
            raise ValueError(f"裁剪结果近似纯色（{key}，box={box}）——截图或坐标框不对")
        cv2.imwrite(str(d / f"{key}.png"), patch)
        saved.append(key)
        if key == "enter1":
            # 只有 enter1 能在主页截图上自校验（enter2/3 属于活动内菜单）。
            # 这里算的是 BAAS 自己的 compare_image 口径（框内 1:1 + rgb 预筛），
            # 自裁结果必然 ≈1.0，用于记录"这份模板来自哪一帧、当时分数多少"。
            verification[key] = {
                **score_in_box(frame, box, patch, ratio),
                "frame": f"{frame.shape[1]}x{frame.shape[0]}",
            }
        else:
            verification[key] = {
                "unverified": "活动内菜单截图，无法在主页画面上校验",
                "frame": f"{frame.shape[1]}x{frame.shape[0]}",
            }
    meta = {
        "module": module,
        "identifier": identifier,
        "created_at": int(time.time()),
        "source": source,
        "boxes": {k: list(v) for k, v in boxes.items() if k in ASSET_KEYS},
        "assets": present_assets(data_dir, identifier, module),
        "verification": verification,
    }
    if extra_meta:
        meta.update(extra_meta)
    with open(d / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return {"saved": saved, "skipped": skipped, "verification": verification, "dir": str(d)}


def delete_patch(data_dir: str | Path, identifier: str, module: str) -> bool:
    """删除某模块的本地资源（还原用；目录不存在返回 False）"""
    import shutil

    try:
        d = module_dir(data_dir, identifier, module)
    except ValueError:
        return False
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True
