"""首次配置检测守护测试。

防止 P0 bug 回归：is_first_run() 只检查 MIMO_API_KEY，漏检其他必填项
（QQBOT_APP_ID/QQBOT_APP_SECRET），导致用户填了 MIMO 但漏配其他时，
向导不触发，主程序启动后报错"没有填"卡死。

必填项：MIMO_API_KEY / QQBOT_APP_ID / QQBOT_APP_SECRET / SILICONFLOW_API_KEY。
EMBED_API_KEY 在迁移本地向量模型（NPU/CPU 内置 BGE）后已改为选填，
为空不再进入首次配置（用户需求：仅真实必填 API 参与判定）。
"""
import setup_wizard


def _set_env_state(monkeypatch, env_exists: bool, vals: dict):
    """配置 is_first_run 的外部依赖（ENV_PATH 存在性 + _load_env_values 返回值）。"""
    monkeypatch.setattr(setup_wizard.os.path, "exists", lambda p: env_exists)
    monkeypatch.setattr(setup_wizard, "_load_env_values", lambda: vals)


def test_first_run_when_env_not_exists(monkeypatch):
    """.env 不存在 → 首次运行。"""
    _set_env_state(monkeypatch, env_exists=False, vals={})
    assert setup_wizard.is_first_run() is True


def test_first_run_when_mimo_set_but_others_missing(monkeypatch):
    """MIMO 有值但其他必填项为空 → 仍应进入首次配置（P0 回归点）。

    旧 bug：只检查 MIMO_API_KEY，返回 False，向导不触发，主程序报错卡死。
    """
    _set_env_state(monkeypatch, env_exists=True, vals={
        "MIMO_API_KEY": "sk-mimo-xxx",
        "QQBOT_APP_ID": "",           # 空
        "QQBOT_APP_SECRET": "secret",
        "EMBED_API_KEY": "embed-key",
    })
    assert setup_wizard.is_first_run() is True, (
        "QQBOT_APP_ID 为空时应进入首次配置，否则主程序会报错卡死"
    )


def test_first_run_when_qqbot_secret_missing(monkeypatch):
    """QQBOT_APP_SECRET 为空 → 首次运行。"""
    _set_env_state(monkeypatch, env_exists=True, vals={
        "MIMO_API_KEY": "sk-mimo-xxx",
        "QQBOT_APP_ID": "12345",
        "QQBOT_APP_SECRET": "",       # 空
        "EMBED_API_KEY": "embed-key",
    })
    assert setup_wizard.is_first_run() is True


def test_first_run_when_sf_missing(monkeypatch):
    """SILICONFLOW_API_KEY 为空 → 首次运行（免费模型发现/重排序核心依赖）。"""
    _set_env_state(monkeypatch, env_exists=True, vals={
        "MIMO_API_KEY": "sk-mimo-xxx",
        "QQBOT_APP_ID": "12345",
        "QQBOT_APP_SECRET": "secret",
        "SILICONFLOW_API_KEY": "",   # 空
    })
    assert setup_wizard.is_first_run() is True, (
        "SILICONFLOW_API_KEY 为必填，为空应进入首次配置"
    )


def test_not_first_run_when_embed_missing(monkeypatch):
    """EMBED_API_KEY 为空 → 不再是首次运行（本地向量模型已内置，选填项）。"""
    _set_env_state(monkeypatch, env_exists=True, vals={
        "MIMO_API_KEY": "sk-mimo-xxx",
        "QQBOT_APP_ID": "12345",
        "QQBOT_APP_SECRET": "secret",
        "SILICONFLOW_API_KEY": "sk-sf-xxx",
        "EMBED_API_KEY": "",          # 空（选填，不应阻塞向导完成）
    })
    assert setup_wizard.is_first_run() is False, (
        "本地向量模型内置后 EMBED_API_KEY 为选填，为空不应强制跳转 setup"
    )


def test_not_first_run_when_all_required_set(monkeypatch):
    """全部必填项（含 SILICONFLOW_API_KEY）都有值 → 不是首次运行。"""
    _set_env_state(monkeypatch, env_exists=True, vals={
        "MIMO_API_KEY": "sk-mimo-xxx",
        "QQBOT_APP_ID": "12345",
        "QQBOT_APP_SECRET": "secret",
        "SILICONFLOW_API_KEY": "sk-sf-xxx",
        "EMBED_API_KEY": "",          # 选填，缺省不影响
    })
    assert setup_wizard.is_first_run() is False


def test_first_run_when_values_are_whitespace_only(monkeypatch):
    """必填项只有空白 → 视为未配置（进入首次配置）。"""
    _set_env_state(monkeypatch, env_exists=True, vals={
        "MIMO_API_KEY": "sk-mimo-xxx",
        "QQBOT_APP_ID": "   ",
        "QQBOT_APP_SECRET": "secret",
        "EMBED_API_KEY": "embed-key",
    })
    assert setup_wizard.is_first_run() is True


def test_first_run_checks_all_required_keys_not_only_mimo(monkeypatch):
    """验证 is_first_run 检查的是全部 REQUIRED_KEYS，而非仅 MIMO_API_KEY。"""
    # 构造一个 MIMO 有值、其他必填项部分缺失的场景
    # 旧实现只查 MIMO 会返回 False（bug），新实现应返回 True
    vals = {"MIMO_API_KEY": "sk-mimo-xxx"}
    # 其余必填项全部缺失
    _set_env_state(monkeypatch, env_exists=True, vals=vals)
    assert setup_wizard.is_first_run() is True
