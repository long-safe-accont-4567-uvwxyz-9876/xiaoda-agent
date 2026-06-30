"""内置工具元数据清单（懒加载用）。

本模块仅依赖标准库与 ``tool_engine.tool_registry.ToolPermission``，**不得** import
任何 ``tools.*`` 子模块或其它重依赖（httpx/selenium/PIL/primp 等），以保证冷启动
时 ``register_builtin_tools_lazy()`` 登记元数据不触发重依赖加载。

每条目字段对应 ``register_lazy_tool`` 的入参，元数据从各 tools 模块 ``@register_tool``
装饰器参数原样复制。``web_browse`` 在 ``web_browse_tools`` 与 ``web_browse_enhanced``
中均被注册，后者覆写前者，故此处仅保留最终生效的 enhanced 版本。
"""
from __future__ import annotations

from typing import Any

from tool_engine.tool_registry import ToolPermission

BUILTIN_TOOLS: list[dict[str, Any]] = [
    # ── tools.file_tools_v2 ──────────────────────────────────────────
    {
        "name": "shell_command",
        "description": "执行 Shell 命令。输入要执行的命令字符串。",
        "schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 命令"}
            },
            "required": ["command"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "system",
        "max_frequency": 30,
        "module_path": "tools.file_tools_v2",
        "func_name": "shell_command",
    },
    {
        "name": "list_files",
        "description": "列出目录中的文件和文件夹。用于查看、整理或操作文件。输入目录路径，默认为当前目录。",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认 ~"}
            },
            "required": [],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "file",
        "module_path": "tools.file_tools_v2",
        "func_name": "list_files",
    },
    {
        "name": "read_file",
        "description": "读取文件内容。输入文件路径。",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "offset": {"type": "integer", "description": "起始行号，默认0", "default": 0},
                "limit": {"type": "integer", "description": "读取行数，默认200", "default": 200}
            },
            "required": ["path"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "file",
        "module_path": "tools.file_tools_v2",
        "func_name": "read_file",
    },
    {
        "name": "write_file",
        "description": "写入文件。输入格式: '文件路径|||内容'",
        "schema": {
            "type": "object",
            "properties": {
                "input_str": {"type": "string", "description": "格式: '文件路径|||内容'"}
            },
            "required": ["input_str"],
        },
        "permission": ToolPermission.READ_WRITE,
        "category": "file",
        "max_frequency": 15,
        "module_path": "tools.file_tools_v2",
        "func_name": "write_file",
    },
    {
        "name": "search_files",
        "description": "搜索文件。输入搜索模式（支持通配符），如 '*.py' 或 '/home/**/*.txt'",
        "schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索模式，支持通配符"}
            },
            "required": ["pattern"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "file",
        "module_path": "tools.file_tools_v2",
        "func_name": "search_files",
    },
    # ── tools.code_tools_v2 ──────────────────────────────────────────
    {
        "name": "get_current_time",
        "description": "获取当前的日期和时间（北京时间 Asia/Shanghai）。无需输入参数。",
        "schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "system",
        "module_path": "tools.code_tools_v2",
        "func_name": "get_current_time",
    },
    {
        "name": "python_executor",
        "description": "执行 Python 代码并返回结果。输入要执行的 Python 代码字符串。可用于计算、数据处理、文件操作等。支持 import 标准库和已安装的第三方库。",
        "schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"}
            },
            "required": ["code"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "code",
        "max_frequency": 5,
        "module_path": "tools.code_tools_v2",
        "func_name": "python_executor",
    },
    {
        "name": "calculator",
        "description": "计算数学表达式。输入数学表达式，如 '2+2' 或 'sqrt(16)'",
        "schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "数学表达式"}
            },
            "required": ["expression"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "code",
        "module_path": "tools.code_tools_v2",
        "func_name": "calculator",
    },
    {
        "name": "call_nahida",
        "description": "向纳西妲姐姐求助。当可莉遇到不懂的问题、需要深度分析、或需要纳西妲姐姐亲自回答时使用此工具。纳西妲姐姐是须弥的草神，温柔聪慧，擅长深度思考和分析。",
        "schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问纳西妲姐姐的问题"}
            },
            "required": ["question"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "fun",
        "module_path": "tools.code_tools_v2",
        "func_name": "call_nahida",
    },
    # ── tools.web_tools_v2 ───────────────────────────────────────────
    {
        "name": "web_search",
        "description": (
            "搜索互联网获取信息。查新闻/时事/最新动态时，请在 query 里带上'最新'或年份等时效词，"
            "会自动切换到新闻引擎（带发布日期和AI综合摘要）。"
            "搜索结果只有标题和摘要——回答前若需要细节，请挑 1-2 条最相关的链接用 web_browse 打开读全文，"
            "不要只凭摘要编造内容。一次搜索没找到，可换不同关键词再搜（中文查不到试英文）。"
            "注意：天气查询用 get_weather，不要用搜索。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "搜索关键词。查时事请带时效词，如'2026世界杯 夺冠热门 最新'"}
            },
            "required": ["query"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "web",
        "max_frequency": 30,
        "module_path": "tools.web_tools_v2",
        "func_name": "web_search",
    },
    {
        "name": "get_weather",
        "description": "获取指定城市的实时天气信息，包括温度、天气状况、风力、湿度等。当用户询问天气、气温、温度、是否下雨/下雪/晴天时，必须调用此工具获取准确数据，不要凭记忆回答。",
        "schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称，如'北京'、'上海'、'武汉'"}
            },
            "required": ["city"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "web",
        "module_path": "tools.web_tools_v2",
        "func_name": "get_weather",
    },
    # ── tools.document_tools ─────────────────────────────────────────
    {
        "name": "document_reader",
        "description": "读取文档内容。支持 PDF、DOCX、PPTX、XLSX 格式。输入文件路径。",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文档文件路径"}
            },
            "required": ["path"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "document",
        "module_path": "tools.document_tools",
        "func_name": "document_reader",
    },
    # ── tools.web_browse_enhanced ────────────────────────────────────
    # web_browse 在 web_browse_tools 与 web_browse_enhanced 中均注册，enhanced 覆写
    # 前者，故 manifest 只保留最终生效的 enhanced 版本。
    {
        "name": "web_browse",
        "description": (
            "打开网页 URL 读取正文全文。这是 web_search 的配套工具："
            "搜索结果只有摘要，挑最相关的链接用本工具读全文后再回答，信息才准确完整。"
            "自动识别国内平台（知乎/B站/微信等）使用专有提取器，"
            "通用网页使用 Jina Reader 高质量提取，最后降级到传统 HTML 解析。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要浏览的网页URL"}
            },
            "required": ["url"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "web",
        "max_frequency": 5,
        "module_path": "tools.web_browse_enhanced",
        "func_name": "web_browse_enhanced",
    },
    # ── tools.multi_search_tools ─────────────────────────────────────
    {
        "name": "wolfram_query",
        "description": (
            "WolframAlpha 知识计算引擎。适用于：1)解方程/不等式（如'solve x^2+3x-4=0'）"
            "2)单位转换（如'100 km/h to mph'）3)科学数据查询（如'boiling point of ethanol'）"
            "4)化学方程式配平/分子量（如'molar mass of H2SO4'）5)物理常数（如'speed of light'）"
            "6)数学函数绘图/微积分（如'integrate sin(x) from 0 to pi'）"
            "注意：简单四则运算用 calculator，搜索新闻/资讯用 web_search，天气用 get_weather。"
            "query 建议用英文以获得最佳结果。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "计算表达式"},
            },
            "required": ["query"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "search",
        "max_frequency": 5,
        "module_path": "tools.multi_search_tools",
        "func_name": "wolfram_query",
    },
    # ── tools.hardware_tools ─────────────────────────────────────────
    {
        "name": "gpio_control",
        "description": "控制 GPIO 引脚。支持设置引脚模式(mode)、写入电平(write)、读取电平(read)。使用 Linux sysfs GPIO 接口。",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["mode", "write", "read"], "description": "操作类型: mode(设置模式), write(写入电平), read(读取电平)"},
                "pin": {"type": "integer", "description": "GPIO 引脚编号"},
                "mode": {"type": "string", "enum": ["in", "out"], "description": "引脚模式: in(输入), out(输出)，action=mode 时必填"},
                "value": {"type": "integer", "enum": [0, 1], "description": "电平值: 0(低电平), 1(高电平)，action=write 时必填"},
            },
            "required": ["action", "pin"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "hardware",
        "max_frequency": 10,
        "module_path": "tools.hardware_tools",
        "func_name": "gpio_control",
    },
    {
        "name": "pwm_control",
        "description": "控制 PWM 脉冲输出。支持启用/禁用 PWM 通道、设置频率和占空比。使用 Linux sysfs PWM 接口。",
        "schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["enable", "disable", "set"],
                    "description": "操作类型: enable(启用PWM), disable(禁用PWM), set(设置频率和占空比)"
                },
                "chip": {"type": "integer", "description": "PWM 芯片编号，默认 0", "default": 0},
                "channel": {"type": "integer", "description": "PWM 通道编号，默认 0", "default": 0},
                "frequency": {"type": "number", "description": "频率(Hz)，action=set 时必填，范围 1-100000"},
                "duty_cycle": {"type": "number", "description": "占空比(%)，action=set 时必填，范围 0-100"},
            },
            "required": ["action"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "hardware",
        "max_frequency": 10,
        "module_path": "tools.hardware_tools",
        "func_name": "pwm_control",
    },
    {
        "name": "i2c_comm",
        "description": "I2C 通信工具。支持扫描总线设备(scan)、读取寄存器(read)、写入寄存器(write)。优先使用 smbus2 库，不可用时回退到 i2ctools 命令行。",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["scan", "read", "write"], "description": "操作类型: scan(扫描设备), read(读取寄存器), write(写入寄存器)"},
                "bus": {"type": "integer", "description": "I2C 总线编号，默认 0", "default": 0},
                "addr": {"type": "integer", "description": "设备地址(十六进制，如 0x68)，read/write 时必填"},
                "register": {"type": "integer", "description": "寄存器地址，read/write 时必填"},
                "length": {"type": "integer", "description": "读取字节数，默认 1", "default": 1},
                "data": {"type": "array", "items": {"type": "integer"}, "description": "写入的数据字节列表，write 时必填"},
            },
            "required": ["action"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "hardware",
        "max_frequency": 10,
        "module_path": "tools.hardware_tools",
        "func_name": "i2c_comm",
    },
    {
        "name": "hardware_status",
        "description": "硬件状态监控工具。用于查询设备运行状况或排查问题。支持查看: all(完整状态), temp(CPU温度), cpu(CPU频率/负载), memory(内存), disk(磁盘), voltage(电压)。",
        "schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["all", "temp", "cpu", "memory", "disk", "voltage"], "description": "监控目标: all(全部), temp(温度), cpu(频率/负载), memory(内存), disk(磁盘), voltage(电压)", "default": "all"},
            },
            "required": [],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "hardware",
        "max_frequency": 30,
        "module_path": "tools.hardware_tools",
        "func_name": "hardware_status",
    },
    # ── tools.vision_tools ───────────────────────────────────────────
    {
        "name": "camera_capture",
        "description": "从USB摄像头拍照。device为设备编号(默认0)，save为True时保存图片到工作目录",
        "schema": {
            "type": "object",
            "properties": {
                "device": {"type": "integer", "description": "摄像头设备编号", "default": 0},
                "width": {"type": "integer", "description": "画面宽度", "default": 640},
                "height": {"type": "integer", "description": "画面高度", "default": 480},
                "save": {"type": "boolean", "description": "是否保存图片到工作目录", "default": False},
            },
            "required": [],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "vision",
        "max_frequency": 5,
        "module_path": "tools.vision_tools",
        "func_name": "camera_capture",
    },
    {
        "name": "vision_analyze",
        "description": "分析摄像头画面。action: detect(目标检测), describe(场景描述), colors(颜色分析)",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["detect", "describe", "colors"], "description": "分析动作: detect(目标检测), describe(场景描述), colors(颜色分析)"},
                "device": {"type": "integer", "description": "摄像头设备编号", "default": 0},
            },
            "required": ["action"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "vision",
        "max_frequency": 3,
        "module_path": "tools.vision_tools",
        "func_name": "vision_analyze",
    },
    # ── tools.system_tools ───────────────────────────────────────────
    {
        "name": "service_manage",
        "description": "系统服务管理工具。支持查看服务状态(status)、重启服务(restart)、列出Agent相关服务(list)、查看服务日志(logs)。",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "restart", "list", "logs"], "description": "操作类型"},
                "name": {"type": "string", "description": "服务名称"},
                "lines": {"type": "integer", "description": "日志行数，默认30", "default": 30},
            },
            "required": ["action"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "system",
        "max_frequency": 10,
        "requires_confirmation": True,
        "module_path": "tools.system_tools",
        "func_name": "service_manage",
    },
    {
        "name": "network_diag",
        "description": "网络诊断工具。支持查看网络接口(interfaces)、测试连通性(ping)、查看监听端口(ports)、测试DNS解析(dns)。",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["interfaces", "ping", "ports", "dns"], "description": "诊断操作类型"},
                "target": {"type": "string", "description": "目标地址，默认8.8.8.8", "default": "8.8.8.8"},
                "count": {"type": "integer", "description": "ping次数，默认3", "default": 3},
            },
            "required": ["action"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "system",
        "max_frequency": 15,
        "module_path": "tools.system_tools",
        "func_name": "network_diag",
    },
    {
        "name": "dev_assist",
        "description": "开发辅助工具。仅在用户明确要求开发调试相关操作时使用。支持查看Git状态(git_status)、检查Python依赖(pip_check)、查看Agent日志(logs)、查看项目结构(project_tree)。",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["git_status", "pip_check", "logs", "project_tree"], "description": "操作类型"},
                "path": {"type": "string", "description": "项目路径", "default": "~/ai-agent"},
                "lines": {"type": "integer", "description": "日志行数，默认50", "default": 50},
                "service": {"type": "string", "description": "服务名称(用于日志)，默认nahida-web", "default": "nahida-web"},
            },
            "required": ["action"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "system",
        "max_frequency": 15,
        "module_path": "tools.system_tools",
        "func_name": "dev_assist",
    },
    # ── tools.agnes_tools ────────────────────────────────────────────
    {
        "name": "agnes_image_generate",
        "description": "使用 AI 生成图片。支持文生图和图生图。",
        "schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "图片描述（英文效果更好）"},
                "image_url": {"type": "string", "description": "参考图片URL（可选，用于图生图）"},
                "size": {"type": "string", "enum": ["1024x1024", "512x512", "1792x1024", "1024x1792"], "default": "1024x1024"},
                "n": {"type": "integer", "default": 1, "description": "生成图片数量"},
            },
            "required": ["prompt"],
        },
        "permission": ToolPermission.READ_ONLY,
        "module_path": "tools.agnes_tools",
        "func_name": "agnes_image_generate",
    },
    {
        "name": "agnes_video_generate",
        "description": "使用 AI 生成视频。支持文生视频，异步任务模式。",
        "schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "视频描述"},
                "seconds": {"type": "number", "default": 5, "description": "视频时长（秒）"},
                "fps": {"type": "integer", "default": 24, "description": "帧率"},
            },
            "required": ["prompt"],
        },
        "permission": ToolPermission.READ_ONLY,
        "module_path": "tools.agnes_tools",
        "func_name": "agnes_video_generate",
    },
    # ── tools.memory_tool ────────────────────────────────────────────
    {
        "name": "remember",
        "description": "保存一条重要记忆。当用户明确要求你记住某件事、纠正你的错误认知、告知个人偏好或重要信息时使用",
        "schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容"},
                "tags": {"type": "string", "description": "标签，用逗号分隔", "default": ""},
                "importance": {"type": "number", "description": "重要程度(0-1)", "default": 0.5},
            },
            "required": ["content"],
        },
        "permission": ToolPermission.READ_WRITE,
        "category": "memory",
        "max_frequency": 5,
        "module_path": "tools.memory_tool",
        "func_name": "remember",
    },
    {
        "name": "recall",
        "description": "检索相关记忆。当用户问到之前聊过的内容、自身配置（如模型版本、系统设置）、用户偏好等不确定的信息时，必须先用此工具查询，不要凭印象编造",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "top_k": {"type": "integer", "description": "返回数量", "default": 5},
            },
            "required": ["query"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "memory",
        "max_frequency": 10,
        "module_path": "tools.memory_tool",
        "func_name": "recall",
    },
    {
        "name": "forget",
        "description": "删除一条记忆",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要忘记的内容关键词"},
            },
            "required": ["query"],
        },
        "permission": ToolPermission.READ_WRITE,
        "category": "memory",
        "max_frequency": 3,
        "module_path": "tools.memory_tool",
        "func_name": "forget",
    },
    # ── tools.nudge_tool ─────────────────────────────────────────────
    {
        "name": "nudge_greeting",
        "description": "发送主动问候消息",
        "schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户ID"},
                "message": {"type": "string", "description": "问候消息（可选）"},
            },
            "required": ["user_id"],
        },
        "permission": ToolPermission.READ_WRITE,
        "category": "social",
        "max_frequency": 1,
        "module_path": "tools.nudge_tool",
        "func_name": "nudge_greeting",
    },
    # ── tools.domestic_search_tools ──────────────────────────────────
    {
        "name": "search_cn",
        "description": (
            "中文互联网搜索——统一搜索入口。根据搜索范围自动选择最佳搜索源："
            "通用搜索(B站+头条)、新闻(头条)、知乎(Bing site:zhihu)、"
            "豆瓣(电影/书籍评分)、B站视频、百度热搜。"
            "scope=auto时自动判断，大多数情况用auto即可。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "scope": {
                    "type": "string",
                    "description": "搜索范围: auto(自动判断)/web(通用)/news(新闻)/hot(热搜)/movie(电影书籍)/zhihu(知乎)/bilibili(B站视频)",
                    "default": "auto",
                },
                "count": {"type": "integer", "description": "返回结果数，默认8", "default": 8},
            },
            "required": ["query"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "web",
        "max_frequency": 30,
        "module_path": "tools.domestic_search_tools",
        "func_name": "search_cn",
    },
    # ── tools.secrets_tool ───────────────────────────────────────────
    {
        "name": "list_secrets",
        "description": "列出当前可用的凭证名（仅返回名称列表，不返回任何凭证值）",
        "schema": {
            "type": "object",
            "properties": {},
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "security",
        "max_frequency": 10,
        "module_path": "tools.secrets_tool",
        "func_name": "list_secrets",
    },
    {
        "name": "use_secret",
        "description": "使用指定凭证执行操作（由 Secrets Broker 代理，调用方不接触原始 API Key）",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "凭证名，例如 OPENAI_API_KEY"},
                "action": {"type": "string", "description": "要执行的操作描述"},
            },
            "required": ["name", "action"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "security",
        "max_frequency": 10,
        "module_path": "tools.secrets_tool",
        "func_name": "use_secret",
    },
    # ── tools.mail_tools ─────────────────────────────────────────────
    {
        "name": "mail_list",
        "description": "列出邮箱中的邮件。可按文件夹（收件箱/已发送/已删除/垃圾邮件）、"
                "时间、是否有附件、是否未读过滤，支持翻页。",
        "schema": {
            "type": "object",
            "properties": {
                "dir": {"type": "string", "enum": ["inbox", "sent", "trash", "spam"],
                        "description": "文件夹，默认 inbox", "default": "inbox"},
                "limit": {"type": "integer", "description": "每页数量，最大50，默认10", "default": 10},
                "cursor": {"type": "string", "description": "翻页游标，来自上一次返回的 next_cursor"},
                "after": {"type": "string", "description": "仅此时间之后的邮件（ISO 8601）"},
                "before": {"type": "string", "description": "仅此时间之前的邮件（ISO 8601）"},
                "has_attachments": {"type": "boolean", "description": "仅显示带附件的邮件"},
                "is_unread": {"type": "boolean", "description": "仅显示未读邮件"},
            },
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "web",
        "max_frequency": 15,
        "module_path": "tools.mail_tools",
        "func_name": "mail_list",
    },
    {
        "name": "mail_read",
        "description": "读取一封邮件的完整内容，包括正文、发件人、收件人、附件元信息等。需要邮件 ID（msg_xxx）。",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "邮件 ID（msg_xxx，来自 mail_list 或 mail_search）"},
            },
            "required": ["id"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "web",
        "max_frequency": 15,
        "module_path": "tools.mail_tools",
        "func_name": "mail_read",
    },
    {
        "name": "mail_search",
        "description": "按关键词和多维度过滤搜索邮件。支持按发件人、收件人、文件夹、时间、附件、未读状态过滤，支持翻页。"
                "翻页时必须保留原搜索条件再追加 cursor。",
        "schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "搜索关键词（必填）"},
                "search_in": {"type": "string",
                              "enum": ["SEARCH_IN_ALL", "SEARCH_IN_SUBJECT", "SEARCH_IN_CONTENT"],
                              "description": "搜索范围：全部/主题/正文", "default": "SEARCH_IN_ALL"},
                "from_addr": {"type": "string", "description": "按发件人过滤"},
                "to": {"type": "string", "description": "按收件人过滤"},
                "dir": {"type": "string", "enum": ["inbox", "sent", "trash", "spam"],
                        "description": "文件夹过滤"},
                "after": {"type": "string", "description": "仅此时间之后（ISO 8601）"},
                "before": {"type": "string", "description": "仅此时间之前（ISO 8601）"},
                "has_attachments": {"type": "boolean", "description": "仅带附件"},
                "is_unread": {"type": "boolean", "description": "仅未读"},
                "limit": {"type": "integer", "description": "每页数量，默认10", "default": 10},
                "cursor": {"type": "string", "description": "翻页游标"},
            },
            "required": ["q"],
        },
        "permission": ToolPermission.READ_ONLY,
        "category": "web",
        "max_frequency": 15,
        "module_path": "tools.mail_tools",
        "func_name": "mail_search",
    },
    {
        "name": "mail_send",
        "description": "发送新邮件（两阶段确认）。首次调用返回操作摘要和确认令牌，需展示给用户；"
                "用户确认后，用相同参数加 confirmation_token 重新调用完成发送。"
                "正文支持 HTML（推荐，可加粗/列表/链接）或纯文本，自动识别。"
                "附件路径必须是相对路径。",
        "schema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "收件人邮箱（必填，可多个）"},
                "subject": {"type": "string", "description": "邮件主题（必填）"},
                "body": {"type": "string", "description": "邮件正文，支持 HTML 或纯文本"},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "抄送"},
                "bcc": {"type": "array", "items": {"type": "string"}, "description": "密送"},
                "attachment": {"type": "array", "items": {"type": "string"},
                               "description": "附件相对路径（最多3个）"},
                "confirmation_token": {"type": "string",
                                       "description": "确认令牌（第二次调用时传入，来自首次调用的返回）"},
            },
            "required": ["to", "subject"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "web",
        "max_frequency": 5,
        "module_path": "tools.mail_tools",
        "func_name": "mail_send",
    },
    {
        "name": "mail_reply",
        "description": "回复邮件（两阶段确认）。首次调用返回摘要和确认令牌，展示给用户；"
                "用户确认后用相同参数加 confirmation_token 重新调用完成回复。"
                "默认仅回复发件人，reply_all=True 回复所有收件人。",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "被回复邮件 ID（msg_xxx，必填）"},
                "body": {"type": "string", "description": "回复正文，支持 HTML 或纯文本"},
                "reply_all": {"type": "boolean", "description": "是否回复全部收件人", "default": False},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "额外抄送"},
                "bcc": {"type": "array", "items": {"type": "string"}, "description": "额外密送"},
                "attachment": {"type": "array", "items": {"type": "string"},
                               "description": "附件相对路径"},
                "confirmation_token": {"type": "string",
                                       "description": "确认令牌（第二次调用时传入）"},
            },
            "required": ["id"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "web",
        "max_frequency": 5,
        "module_path": "tools.mail_tools",
        "func_name": "mail_reply",
    },
    {
        "name": "mail_forward",
        "description": "转发邮件给新收件人（两阶段确认）。首次调用返回摘要和确认令牌，展示给用户；"
                "用户确认后用相同参数加 confirmation_token 重新调用完成转发。"
                "include_attachments=True 可携带原邮件附件。",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "被转发邮件 ID（msg_xxx，必填）"},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "转发收件人（必填，可多个）"},
                "body": {"type": "string", "description": "转发附言，支持 HTML 或纯文本"},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "抄送"},
                "bcc": {"type": "array", "items": {"type": "string"}, "description": "密送"},
                "include_attachments": {"type": "boolean",
                                        "description": "是否携带原邮件附件", "default": False},
                "attachment": {"type": "array", "items": {"type": "string"},
                               "description": "额外附件相对路径"},
                "confirmation_token": {"type": "string",
                                       "description": "确认令牌（第二次调用时传入）"},
            },
            "required": ["id", "to"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "web",
        "max_frequency": 5,
        "module_path": "tools.mail_tools",
        "func_name": "mail_forward",
    },
    {
        "name": "mail_trash",
        "description": "将邮件移到已删除文件夹（两阶段确认，30天后真正删除）。"
                "首次调用返回摘要和确认令牌，展示给用户；用户确认后用相同参数加 confirmation_token 重新调用。"
                "已在已删除文件夹内的邮件不能再调用。",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "邮件 ID（msg_xxx，必填）"},
                "confirmation_token": {"type": "string",
                                       "description": "确认令牌（第二次调用时传入）"},
            },
            "required": ["id"],
        },
        "permission": ToolPermission.EXECUTE,
        "category": "web",
        "max_frequency": 5,
        "module_path": "tools.mail_tools",
        "func_name": "mail_trash",
    },
    {
        "name": "mail_download_attachment",
        "description": "下载邮件的普通附件到本地。仅支持 attachment_id 为 att_xxx 的普通附件；"
                "若 mail_read 返回的是 download_url（超大附件），请勿调用本工具，直接把 download_url 给用户。",
        "schema": {
            "type": "object",
            "properties": {
                "msg": {"type": "string", "description": "邮件 ID（msg_xxx，必填）"},
                "att": {"type": "string", "description": "附件 ID（att_xxx，必填，来自 mail_read）"},
                "output": {"type": "string", "description": "保存目录的相对路径，如 ./downloads（默认当前目录）",
                           "default": "./downloads"},
            },
            "required": ["msg", "att"],
        },
        "permission": ToolPermission.READ_WRITE,
        "category": "web",
        "max_frequency": 10,
        "module_path": "tools.mail_tools",
        "func_name": "mail_download_attachment",
    },
]
