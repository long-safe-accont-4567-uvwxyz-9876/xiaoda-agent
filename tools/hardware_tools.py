from typing import Any, Optional
import os
import shutil
import asyncio
import threading
import time
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult

BLOCKED_PINS = {1, 2, 4, 6, 9, 14, 17, 20, 25, 30, 34, 39}

GPIO_BASE = "/sys/class/gpio"
PWM_BASE = "/sys/class/pwm"

# 硬件状态缓存：5秒TTL（per-target）
_hw_cache: dict | None = None
_hw_cache_lock = threading.Lock()
_hw_cache_ts: dict = {}
_HW_CACHE_TTL = 5.0


def _gpio_path(pin: Any) -> Any:
    """获取GPIO引脚的sysfs路径。"""
    return os.path.join(GPIO_BASE, f"gpio{pin}")


def _gpio_export(pin: Any) -> None:
    """导出GPIO引脚到sysfs接口。"""
    gpio_dir = _gpio_path(pin)
    if not os.path.isdir(gpio_dir):
        with open(os.path.join(GPIO_BASE, "export"), "w") as f:
            f.write(str(pin))


def _gpio_set_direction(pin: Any, mode: Any) -> None:
    """设置GPIO引脚方向（输入或输出）。"""
    with open(os.path.join(_gpio_path(pin), "direction"), "w") as f:
        f.write(mode)


def _gpio_write_value(pin: Any, value: Any) -> None:
    """写入GPIO引脚电平值。"""
    with open(os.path.join(_gpio_path(pin), "value"), "w") as f:
        f.write(str(value))


def _gpio_read_value(pin: Any) -> Any:
    """读取GPIO引脚电平值。"""
    with open(os.path.join(_gpio_path(pin), "value"), "r") as f:
        return f.read().strip()


@register_tool(
    name="gpio_control",
    description="控制 GPIO 引脚。支持设置引脚模式(mode)、写入电平(write)、读取电平(read)。使用 Linux sysfs GPIO 接口。",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["mode", "write", "read"], "description": "操作类型: mode(设置模式), write(写入电平), read(读取电平)"},
            "pin": {"type": "integer", "description": "GPIO 引脚编号"},
            "mode": {"type": "string", "enum": ["in", "out"], "description": "引脚模式: in(输入), out(输出)，action=mode 时必填"},
            "value": {"type": "integer", "enum": [0, 1], "description": "电平值: 0(低电平), 1(高电平)，action=write 时必填"},
        },
        "required": ["action", "pin"],
    },
    permission=ToolPermission.EXECUTE,
    category="hardware",
    max_frequency=10,
)
async def gpio_control(action: str, pin: int, mode: Optional[str] = None, value: Optional[int] = None) -> ToolResult:
    """控制GPIO引脚：设置模式、写入电平或读取电平。"""
    try:
        if not os.path.isdir(GPIO_BASE):
            return ToolResult.fail("GPIO 接口不可用: /sys/class/gpio 不存在。请检查系统是否启用 GPIO 支持。")

        if pin in BLOCKED_PINS:
            return ToolResult.fail(f"引脚 {pin} 为电源/地线引脚，禁止操作！")

        if action == "mode":
            if mode not in ("in", "out"):
                return ToolResult.fail("mode 参数必须为 'in' 或 'out'")
            await asyncio.to_thread(_gpio_export, pin)
            await asyncio.to_thread(_gpio_set_direction, pin, mode)
            mode_label = "输入" if mode == "in" else "输出"
            return ToolResult.ok(f"✅ 引脚 {pin} 已设置为{mode_label}模式")

        if action == "write":
            if value not in (0, 1):
                return ToolResult.fail("value 参数必须为 0 或 1")
            await asyncio.to_thread(_gpio_export, pin)
            await asyncio.to_thread(_gpio_set_direction, pin, "out")
            await asyncio.to_thread(_gpio_write_value, pin, value)
            level_label = "高电平" if value else "低电平"
            return ToolResult.ok(f"✅ 引脚 {pin} 已写入{level_label}({value})")

        if action == "read":
            await asyncio.to_thread(_gpio_export, pin)
            val = await asyncio.to_thread(_gpio_read_value, pin)
            level_label = "高电平" if val == "1" else "低电平"
            return ToolResult.ok(f"📊 引脚 {pin} 当前电平: {level_label}({val})")

        return ToolResult.fail(f"未知操作: {action}，支持的操作: mode, write, read")

    except PermissionError:
        return ToolResult.fail("GPIO 权限不足。请尝试: sudo chmod -R 777 /sys/class/gpio 或将当前用户加入 gpio 用户组。")
    except Exception as e:
        return ToolResult.fail(f"GPIO 操作失败: {e!s}")


# ── PWM 支持 ──────────────────────────────────────────────

def _pwm_chip_path(chip: int) -> str:
    """获取PWM芯片的sysfs路径。"""
    return os.path.join(PWM_BASE, f"pwmchip{chip}")


def _pwm_export(chip: int, channel: int) -> None:
    """导出PWM通道到sysfs接口。"""
    export_path = os.path.join(_pwm_chip_path(chip), "export")
    pwm_dir = os.path.join(_pwm_chip_path(chip), f"pwm{channel}")
    if not os.path.isdir(pwm_dir):
        with open(export_path, "w") as f:
            f.write(str(channel))


def _pwm_unexport(chip: int, channel: int) -> None:
    """取消导出PWM通道。"""
    unexport_path = os.path.join(_pwm_chip_path(chip), "unexport")
    with open(unexport_path, "w") as f:
        f.write(str(channel))


def _pwm_write(chip: int, channel: int, attr: str, value: str) -> None:
    """写入PWM通道属性值。"""
    path = os.path.join(_pwm_chip_path(chip), f"pwm{channel}", attr)
    with open(path, "w") as f:
        f.write(value)


def _pwm_read(chip: int, channel: int, attr: str) -> str:
    """读取PWM通道属性值。"""
    path = os.path.join(_pwm_chip_path(chip), f"pwm{channel}", attr)
    with open(path, "r") as f:
        return f.read().strip()


@register_tool(
    name="pwm_control",
    description="控制 PWM 脉冲输出。支持启用/禁用 PWM 通道、设置频率和占空比。使用 Linux sysfs PWM 接口。",
    schema={
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
    permission=ToolPermission.EXECUTE,
    category="hardware",
    max_frequency=10,
)
async def pwm_control(action: str, chip: int = 0, channel: int = 0,
                      frequency: Optional[float] = None, duty_cycle: Optional[float] = None) -> ToolResult:
    """控制PWM脉冲输出：启用/禁用通道、设置频率和占空比。"""
    try:
        if not os.path.isdir(PWM_BASE):
            return ToolResult.fail("PWM 接口不可用: /sys/class/pwm 不存在。请检查系统是否启用 PWM 支持。")

        chip_path = _pwm_chip_path(chip)
        if not os.path.isdir(chip_path):
            return ToolResult.fail(f"PWM 芯片 {chip} 不存在。可用芯片: {os.listdir(PWM_BASE)}")

        # 导出 PWM 通道
        await asyncio.to_thread(_pwm_export, chip, channel)

        if action == "enable":
            await asyncio.to_thread(_pwm_write, chip, channel, "enable", "1")
            return ToolResult.ok(f"✅ PWM chip{chip}/pwm{channel} 已启用")

        if action == "disable":
            await asyncio.to_thread(_pwm_write, chip, channel, "enable", "0")
            return ToolResult.ok(f"✅ PWM chip{chip}/pwm{channel} 已禁用")

        if action == "set":
            if frequency is None or duty_cycle is None:
                return ToolResult.fail("action=set 时必须提供 frequency 和 duty_cycle 参数")
            if frequency < 1 or frequency > 100000:
                return ToolResult.fail("frequency 范围: 1-100000 Hz")
            if duty_cycle < 0 or duty_cycle > 100:
                return ToolResult.fail("duty_cycle 范围: 0-100%")

            period_ns = int(1_000_000_000 / frequency)
            duty_ns = int(period_ns * duty_cycle / 100)

            await asyncio.to_thread(_pwm_write, chip, channel, "enable", "0")
            await asyncio.to_thread(_pwm_write, chip, channel, "period", str(period_ns))
            await asyncio.to_thread(_pwm_write, chip, channel, "duty_cycle", str(duty_ns))
            await asyncio.to_thread(_pwm_write, chip, channel, "enable", "1")

            return ToolResult.ok(
                f"✅ PWM chip{chip}/pwm{channel} 已设置: "
                f"频率={frequency}Hz (周期={period_ns}ns), "
                f"占空比={duty_cycle}% (高电平={duty_ns}ns)"
            )

        return ToolResult.fail(f"未知操作: {action}，支持: enable, disable, set")

    except PermissionError:
        return ToolResult.fail("PWM 权限不足。请尝试: sudo chmod -R 777 /sys/class/pwm 或将当前用户加入 pwm 用户组。")
    except Exception as e:
        return ToolResult.fail(f"PWM 操作失败: {e!s}")


def _i2c_smbus_read(bus: Any, addr: Any, register: Any, length: Any) -> Any:
    """使用smbus2库读取I2C设备寄存器。"""
    import smbus2
    bus_obj = smbus2.SMBus(bus)
    try:
        if length == 1:
            data = [bus_obj.read_byte_data(addr, register)]
        else:
            data = bus_obj.read_i2c_block_data(addr, register, length)
        return data
    finally:
        bus_obj.close()


def _i2c_smbus_write(bus: Any, addr: Any, register: Any, data: Any) -> None:
    """使用smbus2库写入I2C设备寄存器。"""
    import smbus2
    bus_obj = smbus2.SMBus(bus)
    try:
        if len(data) == 1:
            bus_obj.write_byte_data(addr, register, data[0])
        else:
            bus_obj.write_i2c_block_data(addr, register, data)
    finally:
        bus_obj.close()


def _i2c_smbus_scan(bus: Any) -> Any:
    """使用smbus2库扫描I2C总线上的设备。"""
    import smbus2
    bus_obj = smbus2.SMBus(bus)
    found = []
    try:
        for addr in range(0x03, 0x78):
            try:
                bus_obj.read_byte(addr)
                found.append(addr)
            except (OSError, IOError):
                pass
    finally:
        bus_obj.close()
    return found


async def _i2c_subprocess_scan(bus: Any) -> Any:
    """使用i2cdetect命令扫描I2C总线上的设备。"""
    proc = await asyncio.create_subprocess_exec(
        "i2cdetect", "-y", str(bus),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    if proc.returncode != 0:
        return None
    found = []
    for line in stdout.decode().splitlines():
        parts = line.split()
        for p in parts:
            if p.startswith(("--", "00")) or p == ":":
                continue
            try:
                addr = int(p, 16)
                if 0x03 <= addr <= 0x77:
                    found.append(addr)
            except ValueError:
                logger.debug("hardware_tools.i2c_scan: skipping non-hex token={!r}", p, exc_info=True)
    return found


async def _i2c_subprocess_read(bus: Any, addr: Any, register: Any, length: Any) -> Any:
    """使用i2c工具命令读取I2C设备寄存器。"""
    if length == 1:
        proc = await asyncio.create_subprocess_exec(
            "i2cget", "-y", str(bus), hex(addr), hex(register),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        val = int(stdout.decode().strip(), 16)
        return [val]
    proc = await asyncio.create_subprocess_exec(
        "i2ctransfer", "-y", str(bus), f"w1@{addr}", hex(register), f"r{length}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    if proc.returncode != 0:
        return None
    return [int(x, 16) for x in stdout.decode().strip().split()]


async def _i2c_subprocess_write(bus: Any, addr: Any, register: Any, data: Any) -> Any:
    """使用i2c工具命令写入I2C设备寄存器。"""
    hex_data = " ".join(hex(b) for b in data)
    proc = await asyncio.create_subprocess_exec(
        "i2cset", "-y", str(bus), hex(addr), hex(register), hex_data[0] if len(data) == 1 else hex_data,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=5)
    return proc.returncode == 0


def _has_smbus2() -> bool:
    """检查smbus2库是否可用。"""
    try:
        import smbus2  # noqa: F401
        return True
    except ImportError:
        return False


@register_tool(
    name="i2c_comm",
    description="I2C 通信工具。支持扫描总线设备(scan)、读取寄存器(read)、写入寄存器(write)。优先使用 smbus2 库，不可用时回退到 i2ctools 命令行。",
    schema={
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
    permission=ToolPermission.EXECUTE,
    category="hardware",
    max_frequency=10,
)
async def i2c_comm(action: str, bus: int = 0, addr: Optional[int] = None, register: Optional[int] = None, length: int = 1, data: Optional[list] = None) -> ToolResult:
    """I2C通信：扫描设备、读取或写入寄存器。"""
    try:
        dev_path = f"/dev/i2c-{bus}"
        if not os.path.exists(dev_path):
            return ToolResult.fail(f"I2C 总线 {bus} 不可用: {dev_path} 不存在。请检查 I2C 是否已启用（尝试 sudo raspi-config 或编辑 /boot/config.txt 添加 dtparam=i2c_arm=on）。")

        use_smbus = _has_smbus2()

        if action == "scan":
            if use_smbus:
                found = await asyncio.to_thread(_i2c_smbus_scan, bus)
            else:
                found = await _i2c_subprocess_scan(bus)
                if found is None:
                    return ToolResult.fail("I2C 扫描失败: i2cdetect 命令执行出错。请确认 i2ctools 已安装 (sudo apt install i2c-tools)。")

            if not found:
                return ToolResult.ok(f"🔍 I2C 总线 {bus} 扫描完成: 未发现设备")

            lines = [f"🔍 I2C 总线 {bus} 扫描完成: 发现 {len(found)} 个设备"]
            for a in found:
                lines.append(f"  📌 0x{a:02X}")
            return ToolResult.ok("\n".join(lines))

        if action == "read":
            if addr is None:
                return ToolResult.fail("read 操作需要提供 addr 参数")
            if register is None:
                return ToolResult.fail("read 操作需要提供 register 参数")

            if use_smbus:
                result_data = await asyncio.to_thread(_i2c_smbus_read, bus, addr, register, length)
            else:
                result_data = await _i2c_subprocess_read(bus, addr, register, length)
                if result_data is None:
                    return ToolResult.fail(f"I2C 读取失败: 设备 0x{addr:02X} 寄存器 0x{register:02X} 读取出错")

            hex_vals = " ".join(f"0x{b:02X}" for b in result_data)
            return ToolResult.ok(f"📖 设备 0x{addr:02X} 寄存器 0x{register:02X} 读取 {length} 字节: {hex_vals}")

        if action == "write":
            if addr is None:
                return ToolResult.fail("write 操作需要提供 addr 参数")
            if register is None:
                return ToolResult.fail("write 操作需要提供 register 参数")
            if not data:
                return ToolResult.fail("write 操作需要提供 data 参数")

            if use_smbus:
                await asyncio.to_thread(_i2c_smbus_write, bus, addr, register, data)
                success = True
            else:
                success = await _i2c_subprocess_write(bus, addr, register, data)
                if not success:
                    return ToolResult.fail(f"I2C 写入失败: 设备 0x{addr:02X} 寄存器 0x{register:02X} 写入出错")

            hex_vals = " ".join(f"0x{b:02X}" for b in data)
            return ToolResult.ok(f"✍️ 设备 0x{addr:02X} 寄存器 0x{register:02X} 写入: {hex_vals}")

        return ToolResult.fail(f"未知操作: {action}，支持的操作: scan, read, write")

    except PermissionError:
        return ToolResult.fail("I2C 权限不足。请尝试: sudo usermod -aG i2c $USER 然后重新登录。")
    except FileNotFoundError as e:
        return ToolResult.fail(f"I2C 工具未找到: {e!s}。请安装: sudo apt install i2c-tools python3-smbus2")
    except Exception as e:
        return ToolResult.fail(f"I2C 操作失败: {e!s}")


def _read_sysfs(path: Any) -> Any:
    """读取sysfs文件内容。"""
    with open(path, "r") as f:
        return f.read().strip()


def _read_cpu_temp() -> Any:
    """读取CPU温度。"""
    try:
        raw = _read_sysfs("/sys/class/thermal/thermal_zone0/temp")
        return int(raw) / 1000.0
    except Exception:
        return None


def _read_cpu_freq() -> Any:
    """读取CPU当前频率。"""
    try:
        raw = _read_sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        return int(raw) / 1000.0
    except Exception:
        return None


def _read_loadavg() -> tuple:
    """读取系统负载平均值。"""
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().strip().split()
            return parts[0], parts[1], parts[2]
    except Exception:
        return None, None, None


def _read_memory() -> tuple:
    """读取内存使用信息。"""
    try:
        info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                val = int(parts[1])
                info[key] = val
        total = info.get("MemTotal", 0) * 1024
        available = info.get("MemAvailable", 0) * 1024
        used = total - available
        usage_pct = (used / total * 100) if total > 0 else 0
        return total, used, available, usage_pct
    except Exception:
        return None, None, None, None


def _read_disk() -> tuple:
    """读取磁盘使用信息。"""
    try:
        usage = shutil.disk_usage("/")
        total = usage.total
        free = usage.free
        used = usage.used
        usage_pct = (used / total * 100) if total > 0 else 0
        return total, used, free, usage_pct
    except Exception:
        return None, None, None, None


def _read_voltage() -> Any:
    """读取电源供应器的当前电压值（伏特）。"""
    try:
        for name in os.listdir("/sys/class/power_supply"):
            path = os.path.join("/sys/class/power_supply", name, "voltage_now")
            if os.path.exists(path):
                raw = _read_sysfs(path)
                return int(raw) / 1000000.0
        return None
    except Exception:
        return None


def _fmt_bytes(b: Any) -> str:
    """将字节数格式化为人类可读的字符串（KB/MB/GB）。"""
    if b is None:
        return "N/A"
    if b >= 1073741824:
        return f"{b / 1073741824:.1f} GB"
    if b >= 1048576:
        return f"{b / 1048576:.1f} MB"
    return f"{b / 1024:.1f} KB"


def _read_all_hardware(target: str) -> list[str]:
    """同步读取硬件信息，在线程中执行"""
    lines = []

    if target in ("all", "temp"):
        temp = _read_cpu_temp()
        if temp is not None:
            warn = " ⚠️ WARNING: 温度过高！" if temp > 80 else ""
            lines.append(f"🌡️ CPU 温度: {temp:.1f}°C{warn}")
        else:
            lines.append("🌡️ CPU 温度: 无法读取")

    if target in ("all", "cpu"):
        freq = _read_cpu_freq()
        load1, load5, load15 = _read_loadavg()
        if freq is not None:
            lines.append(f"⚡ CPU 频率: {freq:.0f} MHz")
        else:
            lines.append("⚡ CPU 频率: 无法读取")
        if load1 is not None:
            lines.append(f"📊 CPU 负载: 1min={load1}  5min={load5}  15min={load15}")
        else:
            lines.append("📊 CPU 负载: 无法读取")

    if target in ("all", "memory"):
        total, used, available, usage_pct = _read_memory()
        if total is not None:
            warn = " ⚠️ WARNING: 内存使用率过高！" if usage_pct > 90 else ""
            lines.append(f"💾 内存: 已用 {_fmt_bytes(used)} / 总计 {_fmt_bytes(total)} ({usage_pct:.1f}%){warn}")
            lines.append(f"   可用: {_fmt_bytes(available)}")
        else:
            lines.append("💾 内存: 无法读取")

    if target in ("all", "disk"):
        total, used, free, usage_pct = _read_disk()
        if total is not None:
            lines.append(f"💿 磁盘(/): 已用 {_fmt_bytes(used)} / 总计 {_fmt_bytes(total)} ({usage_pct:.1f}%)")
            lines.append(f"   可用: {_fmt_bytes(free)}")
        else:
            lines.append("💿 磁盘: 无法读取")

    if target in ("all", "voltage"):
        voltage = _read_voltage()
        if voltage is not None:
            lines.append(f"🔋 电压: {voltage:.2f} V")
        else:
            lines.append("🔋 电压: 无法读取")

    return lines


@register_tool(
    name="hardware_status",
    description="硬件状态监控工具。用于查询设备运行状况或排查问题。支持查看: all(完整状态), temp(CPU温度), cpu(CPU频率/负载), memory(内存), disk(磁盘), voltage(电压)。",
    schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "enum": ["all", "temp", "cpu", "memory", "disk", "voltage"], "description": "监控目标: all(全部), temp(温度), cpu(频率/负载), memory(内存), disk(磁盘), voltage(电压)", "default": "all"},
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="hardware",
    max_frequency=30,
)
async def hardware_status(target: str = "all") -> ToolResult:
    """查询硬件状态（温度/CPU/内存/磁盘/电压），带 5 秒缓存。"""
    global _hw_cache, _hw_cache_ts
    try:
        now = time.monotonic()
        with _hw_cache_lock:
            cache_ts = _hw_cache_ts.get(target, 0.0)
            if _hw_cache is not None and (now - cache_ts) < _HW_CACHE_TTL:
                cached = _hw_cache.get(target)
                if cached is not None:
                    return cached

        lines = await asyncio.to_thread(_read_all_hardware, target)
        result = ToolResult.ok("\n".join(lines))

        with _hw_cache_lock:
            if _hw_cache is None:
                _hw_cache = {}
            _hw_cache[target] = result
            _hw_cache_ts[target] = now

        return result
    except Exception as e:
        return ToolResult.fail(f"硬件状态读取失败: {e!s}")