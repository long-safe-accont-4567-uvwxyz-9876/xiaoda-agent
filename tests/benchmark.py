"""小妲 Agent 性能基准测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
import json


def benchmark_context_compression():
    """测试上下文压缩性能"""
    from memory.context_compressor import ContextCompressor

    compressor = ContextCompressor()
    results = {}

    # 1. 工具输出压缩率
    short_output = "Hello, world!" * 10  # ~130 chars
    long_output = "Line {}\n".format("x" * 100) * 100  # ~10000 chars

    compressed_short = compressor.compress_tool_output(short_output, "test_tool")
    compressed_long = compressor.compress_tool_output(long_output, "test_tool")

    results["tool_output"] = {
        "short_input_chars": len(short_output),
        "short_output_chars": len(compressed_short),
        "short_ratio": len(compressed_short) / len(short_output) if short_output else 0,
        "long_input_chars": len(long_output),
        "long_output_chars": len(compressed_long),
        "long_ratio": len(compressed_long) / len(long_output) if long_output else 0,
        "long_savings_pct": round((1 - len(compressed_long) / len(long_output)) * 100, 1) if long_output else 0,
    }

    # 2. 对话历史压缩率
    messages = []
    for i in range(20):
        messages.append({"role": "user", "content": f"这是第{i+1}条用户消息，包含一些测试内容。" * 5})
        messages.append({"role": "assistant", "content": f"这是第{i+1}条助手回复，包含一些测试内容。" * 5})

    original_chars = sum(len(str(m.get("content", ""))) for m in messages)
    compressed_msgs = compressor.compress_history(messages, keep_recent=5)
    compressed_chars = sum(len(str(m.get("content", ""))) for m in compressed_msgs)

    results["history_compression"] = {
        "original_messages": len(messages),
        "compressed_messages": len(compressed_msgs),
        "original_chars": original_chars,
        "compressed_chars": compressed_chars,
        "savings_pct": round((1 - compressed_chars / original_chars) * 100, 1) if original_chars else 0,
    }

    # 3. CCR 检索延迟
    test_content = "Test content for CCR retrieval benchmark" * 100
    compressed = compressor.compress_tool_output(test_content, "bench_tool")
    # 提取 CCR key
    import re
    key_match = re.search(r'key=([a-f0-9]+)', compressed)
    if key_match:
        ccr_key = key_match.group(1)
        start = time.perf_counter()
        for _ in range(100):
            compressor.retrieve(ccr_key)
        elapsed = time.perf_counter() - start
        results["ccr_retrieval"] = {
            "avg_latency_ms": round(elapsed / 100 * 1000, 3),
            "iterations": 100,
        }

    return results


def benchmark_error_classifier():
    """测试错误分类器性能"""
    from utils.error_classifier import ErrorClassifier

    classifier = ErrorClassifier()
    results = {}

    # 分类延迟
    test_errors = [
        Exception("test error"),
        TimeoutError("connection timed out"),
        ValueError("invalid response"),
    ]

    start = time.perf_counter()
    for _ in range(1000):
        for err in test_errors:
            classifier.classify(err)
    elapsed = time.perf_counter() - start

    results["classification_latency"] = {
        "avg_latency_us": round(elapsed / 3000 * 1_000_000, 1),
        "total_classifications": 3000,
    }

    return results


def benchmark_credential_pool():
    """测试凭证池性能"""
    from utils.credential_pool import CredentialPool, Credential

    pool = CredentialPool()
    # 添加测试凭证
    for i in range(5):
        pool.add_credential(Credential(
            api_key=f"test_key_{i}",
            provider="test",
            base_url="https://test.example.com",
        ))

    results = {}

    # 轮换延迟
    start = time.perf_counter()
    for _ in range(1000):
        pool.get_credential("test")
    elapsed = time.perf_counter() - start

    results["rotation_latency"] = {
        "avg_latency_us": round(elapsed / 1000 * 1_000_000, 1),
        "iterations": 1000,
    }

    # 状态转换延迟
    from utils.error_classifier import ClassifiedError, FailoverReason, RecoveryAction
    test_error = ClassifiedError(
        reason=FailoverReason.RATE_LIMIT,
        action=RecoveryAction.BACKOFF_RETRY,
        original_error=Exception("test rate limit"),
        message="rate limit exceeded",
        is_retryable=True,
        backoff_seconds=5.0,
    )
    start = time.perf_counter()
    for _i in range(100):
        pool.report_error("test", test_error)
    elapsed = time.perf_counter() - start

    results["state_transition_latency"] = {
        "avg_latency_us": round(elapsed / 100 * 1_000_000, 1),
        "iterations": 100,
    }

    return results


def benchmark_tool_guardrails():
    """测试工具护栏性能"""
    from tool_engine.tool_guardrails import ToolGuardrails

    guardrails = ToolGuardrails()
    results = {}

    # 检查延迟
    start = time.perf_counter()
    for i in range(1000):
        guardrails.check("test_tool", {"arg": f"value_{i}"})
    elapsed = time.perf_counter() - start

    results["check_latency"] = {
        "avg_latency_us": round(elapsed / 1000 * 1_000_000, 1),
        "iterations": 1000,
    }

    # 记录延迟
    start = time.perf_counter()
    for i in range(1000):
        guardrails.record_call("test_tool", {"arg": f"value_{i}"}, True, "ok")
    elapsed = time.perf_counter() - start

    results["record_latency"] = {
        "avg_latency_us": round(elapsed / 1000 * 1_000_000, 1),
        "iterations": 1000,
    }

    return results


def benchmark_prompt_caching():
    """测试 Prompt Caching 性能"""
    from utils.prompt_caching import apply_cache_control

    results = {}

    # 构建测试消息
    messages = [{"role": "system", "content": "You are a helpful assistant." * 50}]
    for i in range(20):
        messages.append({"role": "user", "content": f"Message {i}" * 10})
        messages.append({"role": "assistant", "content": f"Response {i}" * 10})

    # 缓存标记延迟
    start = time.perf_counter()
    for _ in range(1000):
        apply_cache_control(messages)
    elapsed = time.perf_counter() - start

    results["apply_latency"] = {
        "avg_latency_us": round(elapsed / 1000 * 1_000_000, 1),
        "iterations": 1000,
        "message_count": len(messages),
    }

    return results


def benchmark_atomic_write():
    """测试原子写入性能"""
    import tempfile
    from utils.atomic_write import atomic_write, atomic_json_write

    results = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        # 文本写入延迟
        test_path = Path(tmpdir) / "test.txt"
        test_content = "x" * 10000

        start = time.perf_counter()
        for i in range(100):
            atomic_write(test_path / f"_{i}", test_content)
        elapsed = time.perf_counter() - start

        results["text_write_latency"] = {
            "avg_latency_us": round(elapsed / 100 * 1_000_000, 1),
            "content_size_kb": round(len(test_content) / 1024, 1),
            "iterations": 100,
        }

        # JSON 写入延迟
        test_json_path = Path(tmpdir) / "test.json"
        test_data = {"key": "value" * 1000, "numbers": list(range(100))}

        start = time.perf_counter()
        for i in range(100):
            atomic_json_write(test_json_path / f"_{i}", test_data)
        elapsed = time.perf_counter() - start

        results["json_write_latency"] = {
            "avg_latency_us": round(elapsed / 100 * 1_000_000, 1),
            "iterations": 100,
        }

    return results


def benchmark_hooks():
    """测试钩子系统性能"""
    from hooks import HookEngine, BaseHook, HookType, HookResult

    engine = HookEngine()
    results = {}

    # 空钩子执行延迟（fire_pre_tool_use 是异步方法）
    import asyncio
    loop = asyncio.new_event_loop()
    start = time.perf_counter()
    for _ in range(1000):
        loop.run_until_complete(engine.fire_pre_tool_use("test_tool", {"arg": "value"}))
    elapsed = time.perf_counter() - start

    results["empty_hook_latency"] = {
        "avg_latency_us": round(elapsed / 1000 * 1_000_000, 1),
        "iterations": 1000,
    }

    # 注册自定义钩子后延迟
    class TestHook(BaseHook):
        name = "test_hook"
        hook_type = HookType.PRE_TOOL_USE
        async def execute(self, context):
            return HookResult(allowed=True)

    engine.register(TestHook())

    start = time.perf_counter()
    for _ in range(1000):
        loop.run_until_complete(
            engine.fire_pre_tool_use("test_tool", {"arg": "value"})
        )
    elapsed = time.perf_counter() - start

    results["single_hook_latency"] = {
        "avg_latency_us": round(elapsed / 1000 * 1_000_000, 1),
        "iterations": 1000,
    }

    return results


def run_all_benchmarks():
    """运行所有基准测试"""
    print("=" * 60)
    print("小妲 Agent 性能基准测试报告")
    print("=" * 60)

    all_results = {}

    benchmarks = [
        ("上下文压缩", benchmark_context_compression),
        ("错误分类器", benchmark_error_classifier),
        ("凭证池", benchmark_credential_pool),
        ("工具护栏", benchmark_tool_guardrails),
        ("Prompt Caching", benchmark_prompt_caching),
        ("原子写入", benchmark_atomic_write),
        ("钩子系统", benchmark_hooks),
    ]

    for name, func in benchmarks:
        print(f"\n--- {name} ---")
        try:
            result = func()
            all_results[name] = result
            for key, value in result.items():
                print(f"  {key}:")
                for k, v in value.items():
                    print(f"    {k}: {v}")
        except Exception as e:
            print(f"  错误: {e}")
            all_results[name] = {"error": str(e)}

    # 生成总结
    print("\n" + "=" * 60)
    print("性能优化总结")
    print("=" * 60)

    # 上下文压缩节省
    if "上下文压缩" in all_results and "error" not in all_results["上下文压缩"]:
        tool = all_results["上下文压缩"].get("tool_output", {})
        history = all_results["上下文压缩"].get("history_compression", {})
        ccr = all_results["上下文压缩"].get("ccr_retrieval", {})

        print("\n1. 上下文压缩:")
        print(f"   - 工具输出长文本压缩率: {tool.get('long_savings_pct', 'N/A')}%")
        print(f"   - 对话历史压缩率: {history.get('savings_pct', 'N/A')}%")
        print(f"   - CCR 检索平均延迟: {ccr.get('avg_latency_ms', 'N/A')} ms")

    # 延迟数据
    if "错误分类器" in all_results and "error" not in all_results["错误分类器"]:
        cls = all_results["错误分类器"].get("classification_latency", {})
        print("\n2. 错误分类器:")
        print(f"   - 平均分类延迟: {cls.get('avg_latency_us', 'N/A')} μs")

    if "凭证池" in all_results and "error" not in all_results["凭证池"]:
        rot = all_results["凭证池"].get("rotation_latency", {})
        print("\n3. 凭证池:")
        print(f"   - 轮换平均延迟: {rot.get('avg_latency_us', 'N/A')} μs")

    if "工具护栏" in all_results and "error" not in all_results["工具护栏"]:
        chk = all_results["工具护栏"].get("check_latency", {})
        print("\n4. 工具护栏:")
        print(f"   - 检查平均延迟: {chk.get('avg_latency_us', 'N/A')} μs")

    if "Prompt Caching" in all_results and "error" not in all_results["Prompt Caching"]:
        apply = all_results["Prompt Caching"].get("apply_latency", {})
        print("\n5. Prompt Caching:")
        print(f"   - 缓存标记平均延迟: {apply.get('avg_latency_us', 'N/A')} μs")

    # 保存结果
    result_path = Path(__file__).parent / "benchmark_results.json"
    result_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n详细结果已保存到: {result_path}")

    return all_results


if __name__ == "__main__":
    run_all_benchmarks()
