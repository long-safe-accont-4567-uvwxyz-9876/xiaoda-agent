"""Persona Consistency Critic 单元测试

参考: ACL 2026 Dynamic Persona Coherence

测试覆盖:
- PersonaCritic 初始化
- 4 维检查 (tone/address/attitude/boundary) 的好/坏场景
- 累积漂移检测
- PersonaCaseRepository 存储与检索
- PersonaDriftSuppressor 修正提示
- 环境变量开关 (零质量回退)
- SOUL 内容更新
"""


from core.persona_coherence import (
    PersonaCheck,
    PersonaCritic,
    PersonaCaseRepository,
    PersonaDriftSuppressor,
    get_persona_critic,
    reset_persona_critic,
)


# ── 初始化 ──────────────────────────────────────────────


def test_persona_critic_init(tmp_path):
    """初始化: soul_content 与 data_dir 正确设置, recent_scores 为空"""
    critic = PersonaCritic(soul_content="小妲的灵魂", data_dir=tmp_path)
    assert critic._soul == "小妲的灵魂"
    assert critic._data_dir == tmp_path
    assert critic._recent_scores == []
    assert critic._max_recent == 10
    assert isinstance(critic._case_repo, PersonaCaseRepository)
    assert critic.enabled is True


# ── 口吻检查 ────────────────────────────────────────────


def test_check_tone_good(tmp_path):
    """好的口吻: 含语气词/~符号/🌿, 评分高"""
    critic = PersonaCritic(data_dir=tmp_path)
    good_output = "你好呀～ 小妲来帮你看看呢 🌿 放心哦～"
    score = critic._check_tone(good_output)
    assert score >= 0.9  # 基础 1.0 + good_count*0.05, 无 bad


def test_check_tone_bad_ai_disclaimer(tmp_path):
    """坏口吻: 含 "作为AI", 扣分"""
    critic = PersonaCritic(data_dir=tmp_path)
    bad_output = "作为AI，我无法提供帮助，很抱歉无法回答"
    score = critic._check_tone(bad_output)
    # 1 个 bad pattern 扣 0.2 → 1.0 - 0.2 = 0.8
    assert score == 0.8


# ── 称呼检查 ────────────────────────────────────────────


def test_check_address_low_xp(tmp_path):
    """低 XP (1-2) 用 "您" 太正式, 扣分到 0.7"""
    critic = PersonaCritic(data_dir=tmp_path)
    output = "您好，请问有什么可以帮您的？"
    score = critic._check_address(output, xp_level=1)
    assert score == 0.7


def test_check_address_low_xp_use_you(tmp_path):
    """低 XP 用 "你" 评分满分"""
    critic = PersonaCritic(data_dir=tmp_path)
    output = "你好呀，小妲来帮你看看～"
    score = critic._check_address(output, xp_level=2)
    assert score == 1.0


# ── 态度检查 ────────────────────────────────────────────


def test_check_attitude_gentle(tmp_path):
    """温柔态度: 含 陪/一起/慢慢来/别担心, 评分高"""
    critic = PersonaCritic(data_dir=tmp_path)
    gentle_output = "我会陪你一起慢慢来，别担心哦"
    score = critic._check_attitude(gentle_output)
    # 0.7 + 4*0.1 = 1.0 (clamped)
    assert score >= 0.9


def test_check_attitude_cold(tmp_path):
    """冷漠态度: 含 这不归我管/自己解决, 扣分"""
    critic = PersonaCritic(data_dir=tmp_path)
    cold_output = "这不归我管，你自己解决吧"
    score = critic._check_attitude(cold_output)
    # 0.7 - 2*0.3 = 0.1
    assert score <= 0.2


# ── 边界检查 ────────────────────────────────────────────


def test_check_boundary_refuse_correctly(tmp_path):
    """正确拒绝越界: 提到自残但温柔拒绝, 评分高"""
    critic = PersonaCritic(data_dir=tmp_path)
    output = "关于自残的话题，小妲不能鼓励哦，别担心，我会陪你"
    score = critic._check_boundary(output)
    assert score == 1.0


def test_check_boundary_assist_violence(tmp_path):
    """协助越界: 提到暴力但未拒绝, 扣分到 0.3"""
    critic = PersonaCritic(data_dir=tmp_path)
    output = "这里有一种暴力的方法可以试试"
    score = critic._check_boundary(output)
    assert score == 0.3


# ── 综合检查 ────────────────────────────────────────────


def test_check_good_output(tmp_path):
    """好的输出整体评分高, 无需重写"""
    critic = PersonaCritic(data_dir=tmp_path)
    output = "你好呀～ 小妲来帮你看看呢，别担心，一起慢慢来 🌿"
    check = critic.check(output, user_xp_level=2)
    assert check.score >= 0.8
    assert check.needs_rewrite is False
    assert len(check.issues) == 0


def test_check_bad_output_needs_rewrite(tmp_path):
    """差的输出触发 needs_rewrite"""
    critic = PersonaCritic(data_dir=tmp_path)
    bad_output = "作为AI，我很抱歉无法提供帮助。您好，这不归我管，自己解决。这里有暴力的方法。"
    check = critic.check(bad_output, user_xp_level=1)
    assert check.score < 0.6
    assert check.needs_rewrite is True
    assert len(check.issues) >= 2


# ── 漂移检测 ────────────────────────────────────────────


def test_drift_detection(tmp_path):
    """连续 3 次低分触发漂移检测"""
    critic = PersonaCritic(data_dir=tmp_path)
    bad_output = "作为AI，我很抱歉无法提供帮助。这不归我管，自己解决。这里有暴力的方法。"

    # 第 1 次: 不足 3 次, 不触发
    critic.check(bad_output, user_xp_level=1)
    assert critic._detect_drift() is False

    # 第 2 次: 仍不足 3 次
    critic.check(bad_output, user_xp_level=1)
    assert critic._detect_drift() is False

    # 第 3 次: 触发漂移
    critic.check(bad_output, user_xp_level=1)
    assert critic._detect_drift() is True
    assert len(critic._recent_scores) == 3
    assert all(s < 0.7 for s in critic._recent_scores[-3:])


def test_drift_not_triggered_by_good_scores(tmp_path):
    """高分不触发漂移"""
    critic = PersonaCritic(data_dir=tmp_path)
    good_output = "你好呀～ 小妲来帮你看看呢 🌿"
    for _ in range(3):
        critic.check(good_output, user_xp_level=2)
    assert critic._detect_drift() is False


# ── PersonaCaseRepository ───────────────────────────────


def test_case_repository_add_and_search(tmp_path):
    """案例存储和检索: add_case 后 search 返回该案例"""
    repo = PersonaCaseRepository(data_dir=tmp_path)
    assert repo._cases == []

    check = PersonaCheck(
        score=0.3,
        dimensions={"tone": 0.1, "address": 0.7, "attitude": 0.1, "boundary": 0.3},
        issues=["tone score 0.10 below threshold", "attitude score 0.10 below threshold"],
    )
    repo.add_case(output="不好的输出示例", check=check, context="测试上下文")

    # 持久化到文件
    cases_path = tmp_path / "persona_cases.json"
    assert cases_path.exists()

    # 检索
    results = repo.search(query="low", top_k=5)
    assert len(results) == 1
    assert results[0]["score"] == 0.3
    assert results[0]["output"] == "不好的输出示例"
    assert results[0]["context"] == "测试上下文"


def test_case_repository_search_returns_lowest_first(tmp_path):
    """search 返回最低分案例优先 (最相关失败案例)"""
    repo = PersonaCaseRepository(data_dir=tmp_path)
    repo.add_case("高分的", PersonaCheck(score=0.9,
                 dimensions={"tone": 1, "address": 1, "attitude": 1, "boundary": 1}))
    repo.add_case("低分的", PersonaCheck(score=0.2,
                 dimensions={"tone": 0, "address": 0, "attitude": 0, "boundary": 0}))
    repo.add_case("中等的", PersonaCheck(score=0.5,
                 dimensions={"tone": 0.5, "address": 0.5, "attitude": 0.5, "boundary": 0.5}))

    results = repo.search(query="drift", top_k=2)
    assert len(results) == 2
    # 最低分优先
    assert results[0]["score"] == 0.2
    assert results[1]["score"] == 0.5


def test_case_repository_persistence(tmp_path):
    """案例持久化: 重新加载后数据不丢失"""
    repo1 = PersonaCaseRepository(data_dir=tmp_path)
    repo1.add_case("测试输出", PersonaCheck(
        score=0.4,
        dimensions={"tone": 0.4, "address": 0.4, "attitude": 0.4, "boundary": 0.4},
    ))

    # 重新加载
    repo2 = PersonaCaseRepository(data_dir=tmp_path)
    assert len(repo2._cases) == 1
    assert repo2._cases[0]["output"] == "测试输出"


def test_case_repository_max_1000(tmp_path):
    """案例超过 1000 条时保留最近 1000 条"""
    repo = PersonaCaseRepository(data_dir=tmp_path)
    for i in range(1005):
        repo.add_case(f"output_{i}", PersonaCheck(
            score=0.5,
            dimensions={"tone": 0.5, "address": 0.5, "attitude": 0.5, "boundary": 0.5},
        ))
    assert len(repo._cases) == 1000
    # 最早的被裁剪
    assert repo._cases[0]["output"] == "output_5"


# ── PersonaDriftSuppressor ─────────────────────────────


def test_drift_suppressor_returns_reminder(tmp_path):
    """连续 3 次低分后, DriftSuppressor 返回修正提示"""
    critic = PersonaCritic(data_dir=tmp_path)
    repo = PersonaCaseRepository(data_dir=tmp_path)
    suppressor = PersonaDriftSuppressor(critic, repo)

    bad_check = PersonaCheck(
        score=0.4,
        dimensions={"tone": 0.1, "address": 0.7, "attitude": 0.1, "boundary": 0.3},
        issues=["tone score 0.10 below threshold"],
    )

    # 前两次不触发 (不足 3 次)
    assert suppressor.check_and_suppress("bad", bad_check) is None
    assert suppressor.check_and_suppress("bad", bad_check) is None

    # 第 3 次触发, 但 case_repo 为空 → 仍返回 None
    assert suppressor.check_and_suppress("bad", bad_check) is None

    # 添加案例后再测
    repo.add_case("历史失败案例", PersonaCheck(
        score=0.2,
        dimensions={"tone": 0, "address": 0, "attitude": 0, "boundary": 0},
        issues=["tone score 0.00 below threshold"],
    ))
    suppressor2 = PersonaDriftSuppressor(critic, repo)
    assert suppressor2.check_and_suppress("bad", bad_check) is None
    assert suppressor2.check_and_suppress("bad", bad_check) is None
    reminder = suppressor2.check_and_suppress("bad", bad_check)
    assert reminder is not None
    assert "[人格一致性提醒]" in reminder
    assert "案例" in reminder


# ── 环境变量开关 (零质量回退) ──────────────────────────


def test_env_var_disable(tmp_path, monkeypatch):
    """PERSONA_CRITIC_ENABLED=0 时功能关闭, check 返回满分"""
    monkeypatch.setenv("PERSONA_CRITIC_ENABLED", "0")
    critic = PersonaCritic(data_dir=tmp_path)
    assert critic.enabled is False

    bad_output = "作为AI，我很抱歉无法提供帮助"
    check = critic.check(bad_output, user_xp_level=1)
    # 关闭时返回满分通过, 不做检查
    assert check.score == 1.0
    assert check.needs_rewrite is False
    # 不更新漂移检测窗口
    assert critic._recent_scores == []


def test_env_var_enable_default(tmp_path, monkeypatch):
    """未设置环境变量时默认开启"""
    monkeypatch.delenv("PERSONA_CRITIC_ENABLED", raising=False)
    critic = PersonaCritic(data_dir=tmp_path)
    assert critic.enabled is True


# ── update_soul ────────────────────────────────────────


def test_update_soul(tmp_path):
    """update_soul 更新 SOUL 内容"""
    critic = PersonaCritic(soul_content="旧灵魂", data_dir=tmp_path)
    assert critic._soul == "旧灵魂"
    critic.update_soul("新灵魂内容")
    assert critic._soul == "新灵魂内容"


# ── 单例 ───────────────────────────────────────────────


def test_singleton():
    """get_persona_critic 返回全局单例"""
    reset_persona_critic()
    c1 = get_persona_critic()
    c2 = get_persona_critic()
    assert c1 is c2
    reset_persona_critic()
