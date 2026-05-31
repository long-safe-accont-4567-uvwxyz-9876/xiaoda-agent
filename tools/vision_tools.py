import os
import json
from pathlib import Path
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="vision_analyze",
    description="分析图像内容（使用视觉模型）",
    schema={
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "图像文件路径"},
            "question": {"type": "string", "description": "关于图像的问题", "default": "描述这张图片"},
        },
        "required": ["image_path"],
    },
    permission=ToolPermission.READ_ONLY,
    category="vision",
    max_frequency=5,
)
async def vision_analyze(image_path: str, question: str = "描述这张图片") -> ToolResult:
    try:
        path = Path(image_path).resolve()
        if not path.exists():
            return ToolResult.fail(f"图像文件不存在：{image_path}")
        import base64
        with open(path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()
        suffix = path.suffix.lower()
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(suffix, "image/jpeg")
        from config import load_config
        from openai import AsyncOpenAI
        config = load_config()
        client = AsyncOpenAI(api_key=config["api_key"], base_url=config["base_url"])
        response = await client.chat.completions.create(
            model=config.get("model_name", "Qwen/Qwen3-8B"),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_data}"}},
                ],
            }],
            max_tokens=1000,
        )
        result = response.choices[0].message.content
        return ToolResult.ok(result)
    except Exception as e:
        return ToolResult.fail(f"图像分析失败：{str(e)}")


@register_tool(
    name="capture_camera",
    description="使用摄像头拍摄照片",
    schema={
        "type": "object",
        "properties": {
            "camera_id": {"type": "integer", "description": "摄像头ID", "default": 0},
            "save_path": {"type": "string", "description": "保存路径", "default": "data/camera_capture.jpg"},
        },
        "required": [],
    },
    permission=ToolPermission.EXECUTE,
    category="vision",
    max_frequency=3,
)
async def capture_camera(camera_id: int = 0, save_path: str = "data/camera_capture.jpg") -> ToolResult:
    try:
        import cv2
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        cap = cv2.VideoCapture(camera_id)
        if not cap.isOpened():
            return ToolResult.fail("无法打开摄像头")
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return ToolResult.fail("无法捕获图像")
        cv2.imwrite(save_path, frame)
        return ToolResult.ok(f"照片已保存：{save_path}")
    except ImportError:
        return ToolResult.fail("需要安装 opencv-python：pip install opencv-python")
    except Exception as e:
        return ToolResult.fail(f"摄像头操作失败：{str(e)}")
