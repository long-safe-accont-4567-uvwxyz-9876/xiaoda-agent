from typing import Any

from tool_engine.tool_registry import ToolPermission, ToolResult, register_tool

_vision_service = None


def _get_vision_service() -> Any:
    global _vision_service
    if _vision_service is None:
        from utils.vision_service import VisionService
        _vision_service = VisionService()
    return _vision_service


@register_tool(
    name="camera_capture",
    description="从USB摄像头拍照。device为设备编号(默认0)，save为True时保存图片到工作目录",
    schema={
        "type": "object",
        "properties": {
            "device": {"type": "integer", "description": "摄像头设备编号", "default": 0},
            "width": {"type": "integer", "description": "画面宽度", "default": 640},
            "height": {"type": "integer", "description": "画面高度", "default": 480},
            "save": {"type": "boolean", "description": "是否保存图片到工作目录", "default": False},
        },
        "required": [],
    },
    permission=ToolPermission.EXECUTE,
    category="vision",
    max_frequency=5,
)
def camera_capture(device: int = 0, width: int = 640, height: int = 480, save: bool = False) -> ToolResult:
    try:
        vision_service = _get_vision_service()
        success, frame = vision_service.capture_frame(device, width, height)
        if not success:
            return ToolResult.fail(f"拍照失败：{frame}")
        saved_path = None
        if save:
            saved_path = vision_service.save_frame(frame)
        h, w = frame.shape[:2]
        return ToolResult.ok(f"📸 拍照成功 | 分辨率: {w}x{h} | 保存: {saved_path or '否'}")
    except Exception as e:
        return ToolResult.fail(f"拍照失败: {e!s}")


@register_tool(
    name="vision_analyze",
    description="分析摄像头画面。action: detect(目标检测), describe(场景描述), colors(颜色分析)",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["detect", "describe", "colors"], "description": "分析动作: detect(目标检测), describe(场景描述), colors(颜色分析)"},
            "device": {"type": "integer", "description": "摄像头设备编号", "default": 0},
        },
        "required": ["action"],
    },
    permission=ToolPermission.EXECUTE,
    category="vision",
    max_frequency=3,
)
def vision_analyze(action: str, device: int = 0) -> ToolResult:
    try:
        vision_service = _get_vision_service()
        success, frame = vision_service.capture_frame(device)
        if not success:
            return ToolResult.fail(f"拍照失败：{frame}")

        if action == "detect":
            objects = vision_service.detect_objects(frame)
            n = len(objects)
            obj_list = "\n".join(f"  - {obj}" for obj in objects) if objects else "  (无)"
            return ToolResult.ok(f"🔍 检测到 {n} 个物体:\n{obj_list}")
        if action == "describe":
            description = vision_service.describe_scene(frame)
            return ToolResult.ok(f"👁️ {description}")
        if action == "colors":
            colors = vision_service.analyze_colors(frame)
            color_list = "\n".join(f"  - {c}" for c in colors) if colors else "  (无)"
            return ToolResult.ok(f"🎨 主要颜色:\n{color_list}")
        return ToolResult.fail(f"不支持的分析动作: {action}")
    except Exception as e:
        return ToolResult.fail(f"分析失败: {e!s}")
