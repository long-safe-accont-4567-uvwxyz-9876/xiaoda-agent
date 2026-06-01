import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/home/orangepi/ai-agent")
os.environ.setdefault("SSL_CERT_FILE", "/home/orangepi/miniconda3/lib/python3.13/site-packages/certifi/cacert.pem")

tests = []
passed = failed = 0

def test(name, func):
    global passed, failed
    try:
        func()
        tests.append((name, "PASS", ""))
        passed += 1
    except Exception as e:
        tests.append((name, "FAIL", str(e)[:200]))
        failed += 1

def atest(name, func):
    def wrapper():
        asyncio.run(func())
    test(name, wrapper)


# ========== A. _parse_chat_target 单元测试 ==========
def test_parse_single_mention():
    from agent_core import AgentCore
    core = AgentCore.__new__(AgentCore)
    core._user_chat_target = {}
    result = core._parse_chat_target("@银狼 帮我检查系统", "user1")
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    assert len(result) == 1
    assert result[0] == "yinlang"
test("A1:单@mention→长度1的list", test_parse_single_mention)

def test_parse_multi_mention():
    from agent_core import AgentCore
    core = AgentCore.__new__(AgentCore)
    core._user_chat_target = {}
    result = core._parse_chat_target("@银狼@昔涟 全面检查", "user2")
    assert isinstance(result, list)
    assert len(result) == 2
    assert "yinlang" in result
    assert "xilian" in result
test("A2:多@mention→长度2+顺序正确", test_parse_multi_mention)

def test_parse_natural_language():
    from agent_core import AgentCore
    core = AgentCore.__new__(AgentCore)
    core._user_chat_target = {}
    result = core._parse_chat_target("让银狼帮我写代码", "user3")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == "yinlang"
test("A3:自然语言路由→长度1", test_parse_natural_language)

def test_parse_mixed_with_nahida():
    from agent_core import AgentCore
    core = AgentCore.__new__(AgentCore)
    core._user_chat_target = {}
    result = core._parse_chat_target("@银狼@纳西妲 检查一下", "user4")
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[-1] == "nahida"
test("A4:混合@nahida+其他→nahida在末尾", test_parse_mixed_with_nahida)

def test_parse_empty_input():
    from agent_core import AgentCore
    core = AgentCore.__new__(AgentCore)
    core._user_chat_target = {}
    result = core._parse_chat_target("", "user5")
    assert isinstance(result, list)
    assert result == ["nahida"]
test("A5:空输入→['nahida']", test_parse_empty_input)


# ========== B. RouterNode._rule_route 规则路由测试 ==========
def test_rule_route_single_code():
    from task_orchestrator import RouterNode
    result = RouterNode._rule_route("巡检香橙派")
    assert isinstance(result, list)
    assert "yinlang" in result
    assert len(result) == 1
test("B1:巡检→仅code匹配→不并行", test_rule_route_single_code)

def test_rule_route_parallel_multi():
    from task_orchestrator import RouterNode
    result = RouterNode._rule_route("全面检查系统并搜索最新资讯")
    assert isinstance(result, list)
    assert "yinlang" in result
    assert "xilian" in result
    assert len(result) >= 2
test("B2:全面+多领域→并行返回多个agent", test_rule_route_parallel_multi)

def test_rule_route_research_only():
    from task_orchestrator import RouterNode
    result = RouterNode._rule_route("帮我研究一下深度学习")
    assert isinstance(result, list)
    assert "nike" in result
    assert len(result) == 1
test("B3:研究类→仅nike", test_rule_route_research_only)

def test_rule_route_fallback():
    from task_orchestrator import RouterNode
    result = RouterNode._rule_route("今天天气怎么样")
    assert isinstance(result, list)
    assert result == ["nahida"]
test("B4:无匹配→fallback到nahida", test_rule_route_fallback)


# ========== C. ParallelAgentNode._decompose_task 测试 ==========
async def _test_decompose_two_targets():
    from task_orchestrator import ParallelAgentNode
    node = ParallelAgentNode(dispatcher=None, route_client=None, route_model="")
    agent_configs = {
        "yinlang": {"display_name": "银狼", "route_description": "编程、代码、技术问题"},
        "xilian": {"display_name": "昔涟", "capabilities": ["search"], "route_description": "搜索信息、查询资料"},
    }
    result = await node._decompose_task("全面检查服务器状态", ["yinlang", "xilian"], agent_configs)
    assert isinstance(result, dict)
    assert len(result) == 2
    assert "yinlang" in result
    assert "xilian" in result
    assert "编程" in result["yinlang"] or "技术" in result["yinlang"]
    assert "搜索" in result["xilian"] or "查询" in result["xilian"]

def test_decompose_two_targets():
    asyncio.run(_test_decompose_two_targets())
test("C1:2个目标→生成2个子任务含对应描述", test_decompose_two_targets)

async def _test_decompose_three_targets():
    from task_orchestrator import ParallelAgentNode
    node = ParallelAgentNode(dispatcher=None, route_client=None, route_model="")
    agent_configs = {
        "yinlang": {"route_description": "编程、调试"},
        "xilian": {"route_description": "搜索信息"},
        "nike": {"route_description": "研究分析"},
    }
    result = await node._decompose_task("综合分析项目", ["yinlang", "xilian", "nike"], agent_configs)
    assert len(result) == 3
    for key in result:
        assert "任务" in result[key] or "分析" in result[key]

def test_decompose_three_targets():
    asyncio.run(_test_decompose_three_targets())
test("C2:3个目标→生成3个子任务带序号", test_decompose_three_targets)

async def _test_decompose_single_target():
    from task_orchestrator import ParallelAgentNode
    node = ParallelAgentNode(dispatcher=None, route_client=None, route_model="")
    result = await node._decompose_task("检查内存", ["yinlang"], {})
    assert len(result) == 1
    assert result["yinlang"] == "检查内存"

def test_decompose_single_target():
    asyncio.run(_test_decompose_single_target())
test("C3:1个目标→直接返回原始输入", test_decompose_single_target)


# ========== D. TaskState 数据结构测试 ==========
def test_taskstate_defaults():
    from task_orchestrator import TaskState
    state = TaskState(user_input="test", user_id="u1")
    assert state.route_targets == []
    assert state.route_target == ""
    assert state.intermediate_results == []
    assert state.final_output == ""
test("D1:TaskState默认route_targets为空列表", test_taskstate_defaults)

def test_taskstate_update():
    from task_orchestrator import TaskState
    state = TaskState(user_input="test", user_id="u1")
    state.update({"route_targets": ["yinlang", "xilian"], "final_output": "done"})
    assert len(state.route_targets) == 2
    assert state.final_output == "done"
test("D2:update()正确合并route_targets", test_taskstate_update)


# ========== E. TaskGraph 拓扑测试 ==========
def test_graph_nodes():
    from task_orchestrator import TaskGraph, PARALLEL_EXECUTE, SINGLE_EXECUTE, END
    g = TaskGraph()
    g.add_node("router", lambda s: {})
    g.add_node(PARALLEL_EXECUTE, lambda s: {})
    g.add_node(SINGLE_EXECUTE, lambda s: {})
    g.add_node("synthesis", lambda s: {})
    g.set_entry_point("router")
    g.compile()
    assert len(g._nodes) == 4
    assert PARALLEL_EXECUTE in g._nodes
    assert SINGLE_EXECUTE in g._nodes
test("E1:编译后节点数=4(含parallel+single)", test_graph_nodes)

async def _test_route_condition_single():
    from task_orchestrator import TaskState, route_condition, PARALLEL_EXECUTE, SINGLE_EXECUTE
    state = TaskState(user_input="test", user_id="u1", route_targets=["yinlang"])
    result = await route_condition(state)
    assert result == SINGLE_EXECUTE

def test_route_condition_single():
    asyncio.run(_test_route_condition_single())
test("E2:单目标→SINGLE_EXECUTE", test_route_condition_single)

async def _test_route_condition_parallel():
    from task_orchestrator import TaskState, route_condition, PARALLEL_EXECUTE
    state = TaskState(user_input="test", user_id="u1", route_targets=["yinlang", "xilian"])
    result = await route_condition(state)
    assert result == PARALLEL_EXECUTE

def test_route_condition_parallel():
    asyncio.run(_test_route_condition_parallel())
test("E3:多目标→PARALLEL_EXECUTE", test_route_condition_parallel)

async def _test_route_condition_nahida_only():
    from task_orchestrator import TaskState, route_condition, END
    state = TaskState(user_input="test", user_id="u1", route_targets=["nahida"])
    result = await route_condition(state)
    assert result == END

def test_route_condition_nahida_only():
    asyncio.run(_test_route_condition_nahida_only())
test("E4:仅nahida→END", test_route_condition_nahida_only)

async def _test_route_condition_empty():
    from task_orchestrator import TaskState, route_condition, END
    state = TaskState(user_input="test", user_id="u1", route_targets=[])
    result = await route_condition(state)
    assert result == END

def test_route_condition_empty():
    asyncio.run(_test_route_condition_empty())
test("E5:空targets→END", test_route_condition_empty)


# ========== F. 并行 dispatch 集成测试（mock） ==========
async def _test_parallel_dispatch_normal():
    from task_orchestrator import TaskState, ParallelAgentNode
    mock_agent_yinlang = MagicMock()
    mock_agent_yinlang.available = True
    mock_agent_yinlang.config.display_name = "银狼"
    mock_agent_xilian = MagicMock()
    mock_agent_xilian.available = True
    mock_agent_xilian.config.display_name = "昔涟"

    mock_dispatcher = MagicMock()
    mock_dispatcher.get_agent.side_effect = lambda name: {
        "yinlang": mock_agent_yinlang,
        "xilian": mock_agent_xilian,
    }.get(name)
    mock_dispatcher.dispatch.side_effect = AsyncMock(side_effect=lambda t, p=None, **kw: f"{t}的结果: 系统正常")

    from task_orchestrator import ParallelAgentNode
    node = ParallelAgentNode(dispatcher=mock_dispatcher, route_client=None, route_model="")
    state = TaskState(
        user_input="全面巡检服务器",
        user_id="test_user",
        route_targets=["yinlang", "xilian"],
        _agent_configs={
            "yinlang": {"display_name": "银狼", "route_description": "编程调试"},
            "xilian": {"display_name": "昔涟", "route_description": "搜索查询"},
        },
    )

    result = await node.execute(state)
    assert "intermediate_results" in result
    assert len(result["intermediate_results"]) == 2
    names = [r["display_name"] for r in result["intermediate_results"]]
    assert "银狼" in names
    assert "昔涟" in names
    errors = [r.get("error") for r in result["intermediate_results"]]
    assert not any(errors)

def test_parallel_dispatch_normal():
    asyncio.run(_test_parallel_dispatch_normal())
test("F1:mock 2个agent正常→intermediate有2条无error", test_parallel_dispatch_normal)

async def _test_parallel_dispatch_one_timeout():
    from task_orchestrator import TaskState, ParallelAgentNode
    async def _slow_dispatch(t, *a, **kw):
        if t == "yinlang":
            raise asyncio.TimeoutError()
        return "昔涟搜索完成"

    mock_agent_yinlang = MagicMock()
    mock_agent_yinlang.available = True
    mock_agent_yinlang.config.display_name = "银狼"
    mock_agent_xilian = MagicMock()
    mock_agent_xilian.available = True
    mock_agent_xilian.config.display_name = "昔涟"

    mock_dispatcher = MagicMock()
    mock_dispatcher.get_agent.side_effect = lambda name: {
        "yinlang": mock_agent_yinlang,
        "xilian": mock_agent_xilian,
    }.get(name)
    mock_dispatcher.dispatch.side_effect = _slow_dispatch

    from task_orchestrator import ParallelAgentNode
    node = ParallelAgentNode(dispatcher=mock_dispatcher, route_client=None, route_model="")
    state = TaskState(
        user_input="检查",
        user_id="test",
        route_targets=["yinlang", "xilian"],
        _agent_configs={"yinlang": {"display_name": "银狼", "route_description": "t"}, "xilian": {"display_name": "昔涟", "route_description": "t"}},
    )
    result = await node.execute(state)
    assert len(result["intermediate_results"]) == 2
    yinlang_result = [r for r in result["intermediate_results"] if r["agent"] == "yinlang"][0]
    assert yinlang_result.get("error") is True
    xilian_result = [r for r in result["intermediate_results"] if r["agent"] == "xilian"][0]
    assert xilian_result.get("error") is None

def test_parallel_dispatch_one_timeout():
    asyncio.run(_test_parallel_dispatch_one_timeout())
test("F2:1个超时→该条error=True另一条正常", test_parallel_dispatch_one_timeout)

async def _test_parallel_all_unavailable():
    from task_orchestrator import TaskState, ParallelAgentNode
    mock_dispatcher = MagicMock()
    mock_dispatcher.get_agent.return_value = None

    from task_orchestrator import ParallelAgentNode
    node = ParallelAgentNode(dispatcher=mock_dispatcher, route_client=None, route_model="")
    state = TaskState(
        user_input="test",
        user_id="test",
        route_targets=["yinlang", "xilian"],
        _agent_configs={},
    )
    result = await node.execute(state)
    assert len(result["intermediate_results"]) == 2
    assert all(r.get("error") for r in result["intermediate_results"])

def test_parallel_all_unavailable():
    asyncio.run(_test_parallel_all_unavailable())
test("F3:全部不可用→全部error=True", test_parallel_all_unavailable)

async def _test_parallel_empty_targets():
    from task_orchestrator import TaskState, ParallelAgentNode
    mock_dispatcher = MagicMock()

    from task_orchestrator import ParallelAgentNode
    node = ParallelAgentNode(dispatcher=mock_dispatcher, route_client=None, route_model="")
    state = TaskState(user_input="test", user_id="test", route_targets=[], _agent_configs={})
    result = await node.execute(state)
    assert result.get("sub_agent_reply") == ""
    assert result.get("intermediate_results", []) == []

def test_parallel_empty_targets():
    asyncio.run(_test_parallel_empty_targets())
test("F4:空targets→返回空结果", test_parallel_empty_targets)


# ========== G. SynthesisNode 综合测试（mock） ==========
async def _test_synthesis_single():
    from task_orchestrator import TaskState, SynthesisNode
    mock_client = MagicMock()
    mock_response = MagicMock()
    msg = MagicMock()
    msg.content = "整理后的结果"
    mock_response.choices = [MagicMock(message=msg)]
    mock_client.chat.completions.create.return_value = mock_response

    node = SynthesisNode(client=mock_client, model="test")
    state = TaskState(
        user_input="test",
        user_id="u1",
        intermediate_results=[{"display_name": "银狼", "reply": "系统正常"}],
    )
    result = await node.synthesize(state)
    assert result["final_output"] == "系统正常"

def test_synthesis_single():
    asyncio.run(_test_synthesis_single())
test("G1:单个结果→直接返回", test_synthesis_single)

async def _test_synthesis_multi_with_callback():
    from task_orchestrator import TaskState, SynthesisNode
    callback_called = False
    callback_result = "纳西妲整理完毕：银狼报告系统正常"

    async def fake_callback(prompt):
        nonlocal callback_called
        callback_called = True
        return callback_result

    node = SynthesisNode(client=None, model="test", nahida_chat_callback=fake_callback)
    state = TaskState(
        user_input="test",
        user_id="u1",
        intermediate_results=[
            {"display_name": "银狼", "reply": "CPU 50%, 内存 60%"},
            {"display_name": "昔涟", "reply": "找到3篇相关文档"},
        ],
    )
    result = await node.synthesize(state)
    assert callback_called is True
    assert callback_result in result["final_output"]

def test_synthesis_multi_with_callback():
    asyncio.run(_test_synthesis_multi_with_callback())
test("G2:多个结果→调用nahida_chat_callback", test_synthesis_multi_with_callback)

async def _test_synthesis_callback_exception_fallback():
    from task_orchestrator import TaskState, SynthesisNode
    from unittest.mock import AsyncMock
    async def failing_callback(prompt):
        raise Exception("LLM error")

    mock_client = MagicMock()
    mr = AsyncMock()
    msg = MagicMock()
    msg.content = "fallback summary"
    mr.choices = [MagicMock(message=msg)]
    mock_client.chat.completions.create = AsyncMock(return_value=mr)

    node = SynthesisNode(client=mock_client, model="test", nahida_chat_callback=failing_callback)
    state = TaskState(
        user_input="test",
        user_id="u1",
        intermediate_results=[
            {"display_name": "A", "reply": "result A"},
            {"display_name": "B", "reply": "result B"},
        ],
    )
    result = await node.synthesize(state)
    assert "fallback summary" in result["final_output"]

def test_synthesis_callback_exception_fallback():
    asyncio.run(_test_synthesis_callback_exception_fallback())
test("G3:nahida_chat异常→fallback到LLM拼接", test_synthesis_callback_exception_fallback)


# ========== H. dispatcher _chat_loop fallback 测试 ==========
def test_dispatcher_max_tokens_increased():
    import inspect
    from agent_dispatcher import SubAgent
    source = inspect.getsource(SubAgent._chat_loop)
    assert "max_tokens=800" in source or 'max_tokens=1024 if tools else 800' in source, \
        "max_tokens should be 800 for non-tool rounds"
test("H1:非工具轮max_tokens=800(非300)", test_dispatcher_max_tokens_increased)

def test_dispatcher_forces_summary_on_tool_end():
    import inspect
    from agent_dispatcher import SubAgent
    source = inspect.getsource(SubAgent._chat_loop)
    assert "你已经调用了工具并拿到了结果" in source, \
        "Fallback should force model to summarize tool results"
test("H2:fallback追加system消息强制总结", test_dispatcher_forces_summary_on_tool_end)


# ========== I. agent_core.py 语法完整性检查 ==========
def test_no_undefined_chat_target():
    import ast, textwrap
    with open("/home/orangepi/ai-agent/agent_core.py", "r") as f:
        source = f.read()
    tree = ast.parse(source)
    names_used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names_used.add(node.id)
    targets_assigned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    targets_assigned.add(target.id)
    assert "chat_target" not in names_used or "chat_target" in targets_assigned, \
        "chat_target is used but never defined (NameError at runtime)"
test("I1:无未定义的chat_target引用", test_no_undefined_chat_target)

def test_process_method_references_chat_targets():
    import inspect
    from agent_core import AgentCore
    source = inspect.getsource(AgentCore.process)
    assert "chat_targets =" in source, "process() should use chat_targets (plural)"
    assert "non_nahida_targets" in source, "process() should filter non-nahida targets"
test("I2:process()使用chat_targets复数形式", test_process_method_references_chat_targets)


# ========== Print Results ==========
print("=" * 60)
print("纳西妲 AI Agent 并行调度专项测试 v1.0")
print("=" * 60)
for name, status, err in tests:
    icon = "PASS" if status == "PASS" else "FAIL"
    print(f"  [{icon}] {name}")
    if err:
        print(f"       -> {err}")
print("-" * 60)
print(f"总计: {passed+failed} | 通过: {passed} | 失败: {failed}")
print("=" * 60)
if failed > 0:
    sys.exit(1)
