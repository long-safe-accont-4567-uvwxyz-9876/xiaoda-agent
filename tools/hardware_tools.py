import os
import subprocess
from tool_registry import register_tool, ToolPermission, ToolResult

BLOCKED_PINS = {1, 2, 4, 6, 9, 14, 17, 20, 25, 30, 34, 39}
GPIO_BASE = "/sys/class/gpio"


def _gpio_path(pin):
    return os.path.join(GPIO_BASE, f"gpio{pin}")


def _gpio_export(pin):
    gpio_dir = _gpio_path(pin)
    if not os.path.isdir(gpio_dir):
        with open(os.path.join(GPIO_BASE, "export"), "w") as f:
            f.write(str(pin))


def _gpio_set_direction(pin, mode):
    with open(os.path.join(_gpio_path(pin), "direction"), "w") as f:
        f.write(mode)


def _gpio_write_value(pin, value):
    with open(os.path.join(_gpio_path(pin), "value"), "w") as f:
        f.write(str(value))


def _gpio_read_value(pin):
    with open(os.path.join(_gpio_path(pin), "value"), "r") as f:
        return f.read().strip()


@register_tool(
    name="gpio_control",
    description="控制 GPIO 引脚。支持设置引脚模式(mode)、写入电平(write)、读取电平(read)。使用 Linux sysfs GPIO 接口。",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["mode", "write", "read"], "description": "操作类型"},
            "pin": {"type": "integer", "description": "GPIO 引脚编号"},
            "mode": {"type": "string", "enum": ["in", "out"], "description": "引脚模式"},
            "value": {"type": "integer", "enum": [0, 1], "description": "电平值"},
        },
        "required": ["action", "pin"],
    },
    permission=ToolPermission.EXECUTE,
    category="hardware",
    max_frequency=10,
)
def gpio_control(action: str, pin: int, mode: str = None, value: int = None) -> ToolResult:
    try:
        if not os.path.isdir(GPIO_BASE):
            return ToolResult.fail("GPIO 接口不可用: /sys/class/gpio 不存在。")
        if pin in BLOCKED_PINS:
            return ToolResult.fail(f"引脚 {pin} 为电源/地线引脚，禁止操作！")
        if action == "mode":
            if mode not in ("in", "out"):
                return ToolResult.fail("mode 参数必须为 'in' 或 'out'")
            _gpio_export(pin)
            _gpio_set_direction(pin, mode)
            mode_label = "输入" if mode == "in" else "输出"
            return ToolResult.ok(f"✅ 引脚 {pin} 已设置为{mode_label}模式")
        elif action == "write":
            if value not in (0, 1):
                return ToolResult.fail("value 参数必须为 0 或 1")
            _gpio_export(pin)
            _gpio_set_direction(pin, "out")
            _gpio_write_value(pin, value)
            level_label = "高电平" if value else "低电平"
            return ToolResult.ok(f"✅ 引脚 {pin} 已写入{level_label}({value})")
        elif action == "read":
            _gpio_export(pin)
            val = _gpio_read_value(pin)
            level_label = "高电平" if val == "1" else "低电平"
            return ToolResult.ok(f"📊 引脚 {pin} 当前电平: {level_label}({val})")
        else:
            return ToolResult.fail(f"未知操作: {action}")
    except PermissionError:
        return ToolResult.fail("GPIO 权限不足。")
    except Exception as e:
        return ToolResult.fail(f"GPIO 操作失败: {str(e)}")


def _i2c_smbus_read(bus, addr, register, length):
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


def _i2c_smbus_write(bus, addr, register, data):
    import smbus2
    bus_obj = smbus2.SMBus(bus)
    try:
        if len(data) == 1:
            bus_obj.write_byte_data(addr, register, data[0])
        else:
            bus_obj.write_i2c_block_data(addr, register, data)
    finally:
        bus_obj.close()


def _i2c_smbus_scan(bus):
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


def _i2c_subprocess_scan(bus):
    result = subprocess.run(["i2cdetect", "-y", str(bus)], capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        return None
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        for p in parts:
            if p.startswith("--") or p.startswith("00") or p == ":":
                continue
            try:
                addr = int(p, 16)
                if 0x03 <= addr <= 0x77:
                    found.append(addr)
            except ValueError:
                continue
    return found


def _i2c_subprocess_read(bus, addr, register, length):
    if length == 1:
        result = subprocess.run(["i2cget", "-y", str(bus), hex(addr), hex(register)], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        val = int(result.stdout.strip(), 16)
        return [val]
    else:
        result = subprocess.run(["i2ctransfer", "-y", str(bus), f"w1@{addr}", hex(register), f"r{length}"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        data = [int(x, 16) for x in result.stdout.strip().split()]
        return data


def _i2c_subprocess_write(bus, addr, register, data):
    hex_data = " ".join(hex(b) for b in data)
    result = subprocess.run(["i2cset", "-y", str(bus), hex(addr), hex(register), hex_data[0] if len(data) == 1 else hex_data], capture_output=True, text=True, timeout=5)
    return result.returncode == 0


def _has_smbus2():
    try:
        import smbus2
        return True
    except ImportError:
        return False


@register_tool(
    name="i2c_comm",
    description="I2C 通信工具。支持扫描总线设备(scan)、读取寄存器(read)、写入寄存器(write)。",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["scan", "read", "write"], "description": "操作类型"},
            "bus": {"type": "integer", "description": "I2C 总线编号，默认 0", "default": 0},
            "addr": {"type": "integer", "description": "设备地址"},
            "register": {"type": "integer", "description": "寄存器地址"},
            "length": {"type": "integer", "description": "读取字节数，默认 1", "default": 1},
            "data": {"type": "array", "items": {"type": "integer"}, "description": "写入的数据字节列表"},
        },
        "required": ["action"],
    },
    permission=ToolPermission.EXECUTE,
    category="hardware",
    max_frequency=10,
)
def i2c_comm(action: str, bus: int = 0, addr: int = None, register: int = None, length: int = 1, data: list = None) -> ToolResult:
    try:
        dev_path = f"/dev/i2c-{bus}"
        if not os.path.exists(dev_path):
            return ToolResult.fail(f"I2C 总线 {bus} 不可用: {dev_path} 不存在。")
        use_smbus = _has_smbus2()
        if action == "scan":
            if use_smbus:
                found = _i2c_smbus_scan(bus)
            else:
                found = _i2c_subprocess_scan(bus)
                if found is None:
                    return ToolResult.fail("I2C 扫描失败: i2cdetect 命令执行出错。")
            if not found:
                return ToolResult.ok(f"🔍 I2C 总线 {bus} 扫描完成: 未发现设备")
            lines = [f"🔍 I2C 总线 {bus} 扫描完成: 发现 {len(found)} 个设备"]
            for a in found:
                lines.append(f"  📌 0x{a:02X}")
            return ToolResult.ok("\n".join(lines))
        elif action == "read":
            if addr is None:
                return ToolResult.fail("read 操作需要提供 addr 参数")
            if register is None:
                return ToolResult.fail("read 操作需要提供 register 参数")
            if use_smbus:
                result_data = _i2c_smbus_read(bus, addr, register, length)
            else:
                result_data = _i2c_subprocess_read(bus, addr, register, length)
                if result_data is None:
                    return ToolResult.fail(f"I2C 读取失败: 设备 0x{addr:02X} 寄存器 0x{register:02X} 读取出错")
            hex_vals = " ".join(f"0x{b:02X}" for b in result_data)
            return ToolResult.ok(f"📖 设备 0x{addr:02X} 寄存器 0x{register:02X} 读取 {length} 字节: {hex_vals}")
        elif action == "write":
            if addr is None:
                return ToolResult.fail("write 操作需要提供 addr 参数")
            if register is None:
                return ToolResult.fail("write 操作需要提供 register 参数")
            if not data:
                return ToolResult.fail("write 操作需要提供 data 参数")
            if use_smbus:
                _i2c_smbus_write(bus, addr, register, data)
                success = True
            else:
                success = _i2c_subprocess_write(bus, addr, register, data)
                if not success:
                    return ToolResult.fail(f"I2C 写入失败: 设备 0x{addr:02X} 寄存器 0x{register:02X} 写入出错")
            hex_vals = " ".join(f"0x{b:02X}" for b in data)
            return ToolResult.ok(f"✍️ 设备 0x{addr:02X} 寄存器 0x{register:02X} 写入: {hex_vals}")
        else:
            return ToolResult.fail(f"未知操作: {action}")
    except PermissionError:
        return ToolResult.fail("I2C 权限不足。")
    except FileNotFoundError as e:
        return ToolResult.fail(f"I2C 工具未找到: {str(e)}")
    except Exception as e:
        return ToolResult.fail(f"I2C 操作失败: {str(e)}")


def _read_sysfs(path):
    with open(path, "r") as f:
        return f.read().strip()

def _read_cpu_temp():
    try:
        raw = _read_sysfs("/sys/class/thermal/thermal_zone0/temp")
        return int(raw) / 1000.0
    except Exception:
        return None

def _read_cpu_freq():
    try:
        raw = _read_sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        return int(raw) / 1000.0
    except Exception:
        return None

def _read_loadavg():
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().strip().split()
            return parts[0], parts[1], parts[2]
    except Exception:
        return None, None, None

def _read_memory():
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

def _read_disk():
    try:
        stat = os.statvfs("/")
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bfree * stat.f_frsize
        used = total - free
        usage_pct = (used / total * 100) if total > 0 else 0
        return total, used, free, usage_pct
    except Exception:
        return None, None, None, None

def _read_voltage():
    try:
        for name in os.listdir("/sys/class/power_supply"):
            path = os.path.join("/sys/class/power_supply", name, "voltage_now")
            if os.path.exists(path):
                raw = _read_sysfs(path)
                return int(raw) / 1000000.0
        return None
    except Exception:
        return None

def _fmt_bytes(b):
    if b is None:
        return "N/A"
    if b >= 1073741824:
        return f"{b / 1073741824:.1f} GB"
    elif b >= 1048576:
        return f"{b / 1048576:.1f} MB"
    else:
        return f"{b / 1024:.1f} KB"


@register_tool(
    name="hardware_status",
    description="硬件状态监控工具。支持查看: all(完整状态), temp(CPU温度), cpu(CPU频率/负载), memory(内存), disk(磁盘), voltage(电压)。",
    schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "enum": ["all", "temp", "cpu", "memory", "disk", "voltage"], "description": "监控目标", "default": "all"},
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="hardware",
    max_frequency=30,
)
def hardware_status(target: str = "all") -> ToolResult:
    try:
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
        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.fail(f"硬件状态读取失败: {str(e)}")