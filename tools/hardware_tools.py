import os
import json
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="set_gpio",
    description="设置 GPIO 引脚状态",
    schema={
        "type": "object",
        "properties": {
            "pin": {"type": "integer", "description": "GPIO引脚号"},
            "value": {"type": "integer", "description": "值：0(低电平) 或 1(高电平)", "enum": [0, 1]},
        },
        "required": ["pin", "value"],
    },
    permission=ToolPermission.EXECUTE,
    category="hardware",
    max_frequency=10,
)
async def set_gpio(pin: int, value: int) -> ToolResult:
    try:
        gpio_path = f"/sys/class/gpio/gpio{pin}/value"
        export_path = "/sys/class/gpio/export"
        if not os.path.exists(gpio_path):
            with open(export_path, "w") as f:
                f.write(str(pin))
            direction_path = f"/sys/class/gpio/gpio{pin}/direction"
            with open(direction_path, "w") as f:
                f.write("out")
        with open(gpio_path, "w") as f:
            f.write(str(value))
        return ToolResult.ok(f"GPIO {pin} 已设置为 {value}")
    except Exception as e:
        return ToolResult.fail(f"GPIO 操作失败：{str(e)}")


@register_tool(
    name="get_gpio",
    description="读取 GPIO 引脚状态",
    schema={
        "type": "object",
        "properties": {
            "pin": {"type": "integer", "description": "GPIO引脚号"},
        },
        "required": ["pin"],
    },
    permission=ToolPermission.READ_ONLY,
    category="hardware",
    max_frequency=20,
)
async def get_gpio(pin: int) -> ToolResult:
    try:
        gpio_path = f"/sys/class/gpio/gpio{pin}/value"
        if not os.path.exists(gpio_path):
            export_path = "/sys/class/gpio/export"
            with open(export_path, "w") as f:
                f.write(str(pin))
            direction_path = f"/sys/class/gpio/gpio{pin}/direction"
            with open(direction_path, "w") as f:
                f.write("in")
        with open(gpio_path, "r") as f:
            value = int(f.read().strip())
        return ToolResult.ok({"pin": pin, "value": value})
    except Exception as e:
        return ToolResult.fail(f"GPIO 读取失败：{str(e)}")


@register_tool(
    name="i2c_read",
    description="I2C 总线读取数据",
    schema={
        "type": "object",
        "properties": {
            "bus": {"type": "integer", "description": "I2C总线号", "default": 1},
            "address": {"type": "integer", "description": "设备地址(7位)"},
            "register": {"type": "integer", "description": "寄存器地址"},
            "length": {"type": "integer", "description": "读取字节数", "default": 1},
        },
        "required": ["address"],
    },
    permission=ToolPermission.READ_ONLY,
    category="hardware",
    max_frequency=10,
)
async def i2c_read(bus: int = 1, address: int = 0, register: int = 0, length: int = 1) -> ToolResult:
    try:
        import smbus2
        bus_obj = smbus2.SMBus(bus)
        if register:
            data = bus_obj.read_i2c_block_data(address, register, length)
        else:
            data = [bus_obj.read_byte(address)]
        bus_obj.close()
        return ToolResult.ok({"address": hex(address), "data": data})
    except ImportError:
        return ToolResult.fail("需要安装 smbus2 库：pip install smbus2")
    except Exception as e:
        return ToolResult.fail(f"I2C 读取失败：{str(e)}")


@register_tool(
    name="set_pwm",
    description="设置 PWM 输出",
    schema={
        "type": "object",
        "properties": {
            "channel": {"type": "integer", "description": "PWM通道"},
            "duty_cycle": {"type": "number", "description": "占空比(0-100)"},
            "frequency": {"type": "number", "description": "频率(Hz)", "default": 1000},
        },
        "required": ["channel", "duty_cycle"],
    },
    permission=ToolPermission.EXECUTE,
    category="hardware",
    max_frequency=20,
)
async def set_pwm(channel: int, duty_cycle: float, frequency: float = 1000) -> ToolResult:
    try:
        pwm_path = f"/sys/class/pwm/pwmchip0/pwm{channel}"
        if not os.path.exists(pwm_path):
            with open(f"/sys/class/pwm/pwmchip0/export", "w") as f:
                f.write(str(channel))
        period_ns = int(1e9 / frequency)
        with open(f"{pwm_path}/period", "w") as f:
            f.write(str(period_ns))
        duty_ns = int(period_ns * duty_cycle / 100)
        with open(f"{pwm_path}/duty_cycle", "w") as f:
            f.write(str(duty_ns))
        with open(f"{pwm_path}/enable", "w") as f:
            f.write("1")
        return ToolResult.ok(f"PWM {channel} 已设置：频率={frequency}Hz, 占空比={duty_cycle}%")
    except Exception as e:
        return ToolResult.fail(f"PWM 设置失败：{str(e)}")
