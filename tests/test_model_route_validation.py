"""TDD 测试：模型路由配置验证。

验证 update_route 保存前的模型ID与provider一致性检查，防止用户配置错误。

Issue: A1 - MiniMax/MiniMax-M2.5 配置到 modelscope 导致 LLM 链路大面积失败
Root cause: web/routers/models.py 的 update_route 缺少模型ID验证
"""



def test_validate_model_route_correct_match():
    """正确匹配的模型ID和provider应该通过验证（无警告）。"""
    from web.model_route_validator import validate_model_route
    result = validate_model_route("MiniMaxAI/MiniMax-M2.5", "siliconflow")
    assert result is None or result == ""


def test_validate_model_route_wrong_model_id_prefix():
    """模型ID前缀错误（MiniMax/ 应为 MiniMaxAI/）应该返回警告。"""
    from web.model_route_validator import validate_model_route
    result = validate_model_route("MiniMax/MiniMax-M2.5", "siliconflow")
    assert result is not None
    assert "MiniMaxAI" in result or "前缀" in result or "建议" in result


def test_validate_model_route_provider_mismatch():
    """模型ID正确但provider不匹配应该返回警告。"""
    from web.model_route_validator import validate_model_route
    result = validate_model_route("MiniMaxAI/MiniMax-M2.5", "modelscope")
    assert result is not None
    assert "siliconflow" in result or "provider" in result.lower() or "不匹配" in result


def test_validate_model_route_unknown_model_no_warning():
    """未知模型ID（可能是自定义）不应该报警告。"""
    from web.model_route_validator import validate_model_route
    result = validate_model_route("some-custom-model-id", "siliconflow")
    assert result is None or result == ""


def test_validate_model_route_agnes_builtin():
    """agnes-2.0-flash 与 agnes provider 匹配应该通过。"""
    from web.model_route_validator import validate_model_route
    result = validate_model_route("agnes-2.0-flash", "agnes")
    assert result is None or result == ""


def test_validate_model_route_mimo_builtin():
    """mimo-v2.5 与 mimo provider 匹配应该通过。"""
    from web.model_route_validator import validate_model_route
    result = validate_model_route("mimo-v2.5", "mimo")
    assert result is None or result == ""
