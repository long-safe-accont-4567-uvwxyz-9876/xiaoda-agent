#!/usr/bin/env python3
"""SiliconFlow MiniMax-M2.5 模型可用性测试

验证 A1 修复后的模型路由配置是否正确：
- MiniMaxAI/MiniMax-M2.5 (SiliconFlow 正确格式)
- MiniMax/MiniMax-M2.5 (modelscope 错误格式，应失败)
- Pro/MiniMaxAI/MiniMax-M2.5 (Pro 变体)
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import httpx

BASE_URL = "https://api.siliconflow.cn/v1"

MODELS_TO_TEST = [
    "MiniMaxAI/MiniMax-M2.5",
    "MiniMax/MiniMax-M2.5",
    "Pro/MiniMaxAI/MiniMax-M2.5",
    "deepseek-ai/DeepSeek-V3-0324",
    "Qwen/Qwen2.5-7B-Instruct",
]

payload_template = {
    "messages": [{"role": "user", "content": "说一个字：好"}],
    "max_tokens": 10,
    "temperature": 0.0,
}

def main() -> None:
    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise SystemExit("SILICONFLOW_API_KEY 未设置")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    print("=" * 70)
    print("SiliconFlow MiniMax-M2.5 模型可用性测试")
    print("=" * 70)
    print(f"Base URL: {BASE_URL}")
    print()
    results = []
    for model in MODELS_TO_TEST:
        payload = {**payload_template, "model": model}
        print(f"[测试] {model}")
        start = time.time()
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    f"{BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            elapsed = time.time() - start
            status = resp.status_code
            if status == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                model_used = data.get("model", "?")
                print(f"  OK ({status}) - {elapsed:.2f}s")
                print(f"     response: {content[:30]}")
                print(f"     model_used: {model_used}")
                results.append({
                    "model": model, "status": "OK", "code": status,
                    "latency": round(elapsed, 2), "response": content[:30],
                })
            else:
                try:
                    err_data = resp.json()
                    err_msg = err_data.get("error", {}).get("message", resp.text[:100])
                except Exception:
                    err_msg = resp.text[:100]
                print(f"  FAIL ({status}) - {elapsed:.2f}s")
                print(f"     error: {err_msg[:100]}")
                results.append({
                    "model": model, "status": "FAIL", "code": status,
                    "latency": round(elapsed, 2), "error": err_msg[:100],
                })
        except Exception as e:
            elapsed = time.time() - start
            err_str = f"{type(e).__name__}: {str(e)[:100]}"
            print(f"  ERROR - {elapsed:.2f}s")
            print(f"     {err_str}")
            results.append({
                "model": model, "status": "ERROR", "code": 0,
                "latency": round(elapsed, 2), "error": err_str,
            })
        print()
    print("=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    ok_count = sum(1 for result in results if result["status"] == "OK")
    fail_count = len(results) - ok_count
    print(f"成功: {ok_count}/{len(results)}  失败: {fail_count}/{len(results)}")
    print()
    for result in results:
        status_icon = "OK" if result["status"] == "OK" else "FAIL"
        latency = f"{result['latency']:.2f}s"
        code = result.get("code", "?")
        print(f"  [{status_icon:4s}] {result['model']:42s} code={code:<3} latency={latency:>6s}")


if __name__ == "__main__":
    main()
