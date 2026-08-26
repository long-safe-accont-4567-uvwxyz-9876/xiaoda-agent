"""真实模型批量跑分 runner：golden cases × (baseline, candidate) 两版执行。

baseline 用 profile 绑定的内置模板渲染（.replace 占位符，容忍模板内
JSON 示例的花括号）；candidate 优先用 staged override（经 repository
严格变量校验）。model 参数可注入 fake 以做确定性测试，默认走
core.router.route("chat")。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from web.prompt_ab import compare_runs, promote_gate
from web.prompt_profiles import profile_by_id

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MARKER = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")
SWEEP_BACKENDS = ("current", "api", "local")
# 单次模型调用上限，对齐 message_processor 的 LLM_CALL_TIMEOUT 惯例
_INVOKE_TIMEOUT = 30.0


def render_builtin_templates(
    prompt_id: str,
    variables: dict[str, str],
) -> tuple[str, str]:
    """渲染内置 production 模板为 (system, user)。未绑定模板抛 ValueError。"""
    profile = profile_by_id(prompt_id)
    if profile is None:
        raise ValueError(f"unknown prompt profile: {prompt_id}")
    if not profile.template_refs:
        raise ValueError(f"prompt has no bound builtin templates: {prompt_id}")
    from web.prompt_profiles import _resolve_template_ref

    templates = [_resolve_template_ref(ref) for ref in profile.template_refs]
    rendered = []
    for template in templates[:2]:
        text = template
        for token in _PLACEHOLDER.findall(template):
            if token in variables:
                text = text.replace(f"{{{token}}}", str(variables[token]))
        for token in _MARKER.findall(template):
            if token in variables:
                text = text.replace(f"<<{token}>>", str(variables[token]))
        rendered.append(text)
    if len(rendered) == 1:
        # 单 ref 槽位由 profile.system_slot 决定：intent.decompose 治理
        # 对象是 system 消息，其余节点模板本质是 user 内容
        if profile.system_slot:
            return rendered[0], ""
        return "", rendered[0]
    return rendered[0], rendered[1]


async def run_prompt_ab(
    core: Any,
    repository: Any,
    prompt_id: str,
    *,
    backend: str = "current",
    node_local_model: str | None = None,
    model: Any = None,
    baseline_label: str = "builtin",
    candidate_label: str = "candidate",
) -> dict[str, Any]:
    """对 golden cases 全量执行两版提示词并产出对比报告与门禁结论。

    backend=current 走主 chat 路由；api=硅基流动免费模型；local=本地对话
    模型（node_local_model 为节点独立选择的模型 ID）。同一轮 A/B 两版必须
    同后端同参数，跨后端结论只能通过 sweep 分路对比获得。
    无 staged 候选时 no_candidate_under_test=True 且门禁置为未通过——
    candidate==baseline 的"全绿"是空洞结论，不得据此 promote。
    """
    profile = profile_by_id(prompt_id)
    if profile is None:
        raise ValueError(f"unknown prompt profile: {prompt_id}")
    node_id = _node_for_prompt(prompt_id)
    from web.prompt_golden_cases import golden_cases_for_node

    cases = golden_cases_for_node(node_id or "")
    if not cases:
        raise ValueError(f"no golden cases for node: {node_id}")

    invoke, backend_errors = build_backend_model(
        backend, core, node_local_model=node_local_model, model=model)

    baseline_outputs: dict[str, str] = {}
    candidate_outputs: dict[str, str] = {}
    staged_any = False
    for case in cases:
        system, user = render_builtin_templates(prompt_id, case.variables)
        baseline_outputs[case.case_id] = await _safe_invoke(invoke, system, user, backend_errors)
        staged_rendered = repository.render_staged(prompt_id, case.variables)
        if staged_rendered is not None:
            staged_any = True
            c_system, c_user = staged_rendered
            candidate_outputs[case.case_id] = await _safe_invoke(
                invoke, c_system, c_user, backend_errors)
        else:
            candidate_outputs[case.case_id] = baseline_outputs[case.case_id]
    report = compare_runs(
        cases, baseline_outputs, candidate_outputs,
        baseline_label=baseline_label, candidate_label=candidate_label,
    )
    passed, reasons = promote_gate(report)
    if backend_errors:
        reasons = [*reasons, *backend_errors]
    if not staged_any:
        reasons.append("no candidate staged under test; gate verdict pending")
    return {
        "prompt_id": prompt_id,
        "node_id": node_id,
        "backend": backend,
        "report": report,
        "no_candidate_under_test": not staged_any,
        "gate": {
            "passed": passed and not backend_errors and staged_any,
            "reasons": reasons,
        },
    }


async def _safe_invoke(invoke: Any, system: str, user: str,
                       errors: list[str]) -> str:
    try:
        return await asyncio.wait_for(invoke(system, user), timeout=_INVOKE_TIMEOUT)
    except asyncio.TimeoutError:
        message = f"model call timed out after {_INVOKE_TIMEOUT:.0f}s"
        if message not in errors:
            errors.append(message)
        return ""
    except Exception as e:
        message = f"model call failed: {e}"
        if message not in errors:
            errors.append(message)
        return ""


def build_backend_model(
    backend: str,
    core: Any,
    *,
    node_local_model: str | None = None,
    model: Any = None,
) -> tuple[Any, list[str]]:
    """返回 (invoke(system,user)->str, errors) —— errors 列表在调用中累积。"""
    if model is not None:
        errors: list[str] = []
        return model, errors

    async def current_model(system: str, user: str) -> str:
        router = getattr(core, "router", None)
        if router is None:
            raise RuntimeError("core.router is not available")
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user},
        ]
        output = await router.route("chat", messages, temperature=0, max_tokens=1024)
        return output if isinstance(output, str) else str(output)

    async def api_model(system: str, user: str) -> str:
        import asyncio

        from utils.free_model_backend import FreeModelBackend

        free = FreeModelBackend()
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user},
        ]
        output = await free.call(messages, temperature=0, max_tokens=1024)
        if output is None:
            await asyncio.sleep(15)
            output = await free.call(messages, temperature=0, max_tokens=1024)
        if output is None:
            await asyncio.sleep(30)
            output = await free.call(messages, temperature=0, max_tokens=1024)
        if output is None:
            raise RuntimeError("api free model unavailable (missing key or request failed)")
        return output

    async def local_model(system: str, user: str) -> str:
        from utils.free_model_backend import call_local_model

        router = getattr(core, "router", None) if core is not None else None
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user},
        ]
        output = await call_local_model(
            router, messages, 0, 1024, model_id=node_local_model,
        )
        if output is None:
            raise RuntimeError("local chat model unavailable")
        return output

    builders = {
        "current": lambda: current_model,
        "api": lambda: api_model,
        "local": lambda: local_model,
    }
    factory = builders.get(backend)
    if factory is None:
        raise ValueError(f"invalid backend for prompt AB: {backend}")
    return factory(), []


async def run_prompt_ab_multi(
    core: Any,
    repository: Any,
    prompt_id: str,
    *,
    runs: int = 3,
    backend: str = "current",
    node_local_model: str | None = None,
    model: Any = None,
) -> dict[str, Any]:
    """N 次独立跑分后按保守语义聚合（偶发失败视为不稳定）。

    聚合门禁拒绝“抽签式通过”：候选必须在全部 N 轮都满足 schema/golden，
    任一轮出现的回归、违禁或丢字面量都会进入合并结论。
    """
    from web.prompt_ab import merge_run_summaries, promote_gate

    if runs < 1 or runs > 5:
        raise ValueError(f"runs must be within 1..5: {runs}")
    run_reports = []
    for _ in range(runs):
        single = await run_prompt_ab(
            core, repository, prompt_id,
            backend=backend, node_local_model=node_local_model, model=model,
        )
        run_reports.append(single)
    invalid = [
        r for r in run_reports
        if any(rr.startswith("model call failed") for rr in r["gate"]["reasons"])
    ]
    if invalid:
        infra_reasons = [
            rr for r in invalid for rr in r["gate"]["reasons"]
            if rr.startswith("model call failed")
        ]
        raise RuntimeError(
            f"model infra failure in {len(invalid)}/{runs} rounds; "
            f"retry when backend is healthy: {infra_reasons[0]}"
        )
    merged_report = merge_run_summaries([r["report"] for r in run_reports])
    passed, reasons = promote_gate(merged_report)
    staged_any = all(not r["no_candidate_under_test"] for r in run_reports)
    if not staged_any:
        reasons.append("no candidate staged under test; gate verdict pending")
    return {
        "prompt_id": prompt_id,
        "node_id": run_reports[0]["node_id"],
        "backend": backend,
        "runs": runs,
        "per_run_candidate_all_ok": [
            r["report"]["candidate"]["all_ok"] for r in run_reports
        ],
        "no_candidate_under_test": not staged_any,
        "report": merged_report,
        "gate": {"passed": passed and staged_any, "reasons": reasons},
    }


async def run_prompt_ab_sweep(
    core: Any,
    repository: Any,
    prompt_id: str,
    backends: tuple[str, ...] = ("current",),
    *,
    node_local_model: str | None = None,
    model: Any = None,
) -> dict[str, Any]:
    """多后端分路标定：每个后端独立跑完整 A/B，门禁要求逐后端全部通过。"""
    invalid = [b for b in backends if b not in SWEEP_BACKENDS]
    if invalid:
        raise ValueError(f"invalid backends: {invalid}")
    sweeps = []
    combined_reasons: list[str] = []
    for backend in backends:
        result = await run_prompt_ab(
            core, repository, prompt_id,
            backend=backend, node_local_model=node_local_model, model=model,
        )
        sweeps.append(result)
        combined_reasons.extend(f"[{backend}] {r}" for r in result["gate"]["reasons"])
    passed = bool(sweeps) and all(s["gate"]["passed"] for s in sweeps)
    return {
        "prompt_id": prompt_id,
        "node_id": sweeps[0]["node_id"] if sweeps else None,
        "backends": list(backends),
        "sweeps": sweeps,
        "gate": {"passed": passed, "reasons": combined_reasons},
    }


def _node_for_prompt(prompt_id: str) -> str | None:
    from web.prompt_profiles import NODE_PROMPT_PROFILES

    for node_id, profiles in NODE_PROMPT_PROFILES.items():
        if any(profile.prompt_id == prompt_id for profile in profiles):
            return node_id
    return None
