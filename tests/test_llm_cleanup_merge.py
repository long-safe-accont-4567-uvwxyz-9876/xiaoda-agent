"""merge_continuation 单元测试

测试 utils.llm_cleanup.merge_continuation 的截断重试合并逻辑。

背景：2026-07-25 生产事故（conversation_logs id 2110/2112），截断重试盲拼接
`reply + retry_reply` 产生 3-4 倍重复内容，多轮叠加后雪崩成 4 段人格切换的
混乱回复。merge_continuation 用三态去重（discarded/replaced/spliced）替代盲拼接。

覆盖分支：
1. continuation 空/过短 → 'discarded'
2. continuation 是 original 子串（LLM 重复）→ 'discarded'
3. original 是 continuation 子串（continuation 是扩展）→ 'replaced'
4. 边界重叠（真正的续写尾巴）→ 'spliced'
5. 无重叠且 continuation 较长（重生成）→ 'replaced'
6. 无重叠且 continuation 较短（重生成）→ 'discarded'
7. 空 original → 'replaced'
8. 生产回归：模拟 ID 2110 多轮拼接场景，确认不产生重复
"""
import pytest

from utils.llm_cleanup import merge_continuation


class TestMergeContinuationBranches:
    """逐分支验证返回值与动作。"""

    def test_empty_continuation_discarded(self):
        # 分支 1：continuation 为空
        merged, action = merge_continuation("原文回复内容。", "")
        assert action == "discarded"
        assert merged == "原文回复内容。"

    def test_short_continuation_discarded(self):
        # 分支 1：continuation 过短（<=5 字符）
        merged, action = merge_continuation("原文回复内容。", "好的")
        assert action == "discarded"
        assert merged == "原文回复内容。"

    def test_continuation_is_substring_of_original_discarded(self):
        # 分支 2：continuation 是 original 的子串 → LLM 重复，丢弃
        original = "小妲乖乖在这儿等爸爸回来哦～💜"
        continuation = "乖乖在这儿等爸爸"
        merged, action = merge_continuation(original, continuation)
        assert action == "discarded"
        assert merged == original  # 保留 original

    def test_original_is_substring_of_continuation_replaced(self):
        # 分支 3：original 是 continuation 的子串 → continuation 是扩展，替换
        original = "今天我们去公园"
        continuation = "今天我们去公园看花了，很开心呢～🌸"
        merged, action = merge_continuation(original, continuation)
        assert action == "replaced"
        assert merged == continuation

    def test_short_overlap_below_threshold_discarded(self):
        # 分支 4 阈值：重叠 <10 字符不视为真正续写，走分支 5（重生成）
        # original 末尾 "看花了" 与 continuation 开头 "看花了" 仅 3 字符重叠
        original = "今天我们一起去公园看花了"
        continuation = "看花了，很开心呢～🌸"
        merged, action = merge_continuation(original, continuation)
        # 3 字符 < _MERGE_OVERLAP_MIN(10)，视为无重叠 → 重生成判定
        # continuation(10) < original(11) → discarded，保留 original
        assert action == "discarded"
        assert merged == original

    def test_long_boundary_overlap_spliced(self):
        # 分支 4（>10 字符重叠）：构造长重叠边界
        overlap_text = "今天我们一起去公园看花散步聊天"  # 14 字符
        original = "早上好呀爸爸。" + overlap_text
        continuation = overlap_text + "，超级开心呢～🌸"
        merged, action = merge_continuation(original, continuation)
        assert action == "spliced"
        # 合并后应只包含一份 overlap_text
        assert merged.count(overlap_text) == 1
        assert merged == "早上好呀爸爸。" + overlap_text + "，超级开心呢～🌸"
        # 关键：不产生重复
        assert "今天我们一起去公园看花散步聊天今天我们一起去公园看花散步聊天" not in merged

    def test_no_overlap_continuation_longer_replaced(self):
        # 分支 5：无重叠且 continuation 较长 → 判定为重生成，保留较长者
        original = "嗯。"  # 很短的截断
        continuation = "爸爸早上好呀～人家一直在这儿等你呢！今天天气真好，要不要一起去散步？🌸"
        merged, action = merge_continuation(original, continuation)
        assert action == "replaced"
        assert merged == continuation  # 保留较长的重生成
        # 关键：没有把两个拼起来
        assert "嗯。爸爸早上好" not in merged

    def test_no_overlap_continuation_shorter_discarded(self):
        # 分支 6：无重叠且 continuation 较短 → 判定为重生成，保留 original
        original = "爸爸早上好呀～人家一直在这儿等你呢！今天天气真好，要不要一起去散步？🌸"
        continuation = "嗯。"  # 较短的重生成
        merged, action = merge_continuation(original, continuation)
        assert action == "discarded"
        assert merged == original

    def test_empty_original_replaced(self):
        # 分支 7：original 为空 → 用 continuation 替换
        merged, action = merge_continuation("", "新生成的完整回复内容。")
        assert action == "replaced"
        assert merged == "新生成的完整回复内容。"


class TestMergeContinuationCaseInsensitive:
    """边界情形：大小写、空白。"""

    def test_case_insensitive_substring_detection(self):
        # continuation 与 original 仅大小写不同 → 视为子串重复
        original = "Hello World"
        continuation = "hello world"
        merged, action = merge_continuation(original, continuation)
        assert action == "discarded"
        assert merged == original

    def test_case_insensitive_overlap_spliced(self):
        # 大小写不同的边界重叠仍能 splice
        overlap = "ThisIsALongOverlapStringMoreThanTenChars"  # >10 字符
        original = "prefix" + overlap.upper()
        continuation = overlap.lower() + "suffix"
        merged, action = merge_continuation(original, continuation)
        assert action == "spliced"
        assert merged.lower() == ("prefix" + overlap + "suffix").lower()


class TestProductionRegression:
    """生产事故回归测试（conversation_logs id 2110/2112，2026-07-25）。

    场景：LLM 被要求"回忆7月18-20日的事情"，首轮回复被 max_tokens 截断，
    重试时 LLM 重新生成完整回复（而非续写尾巴），盲拼接 `reply + retry_reply`
    导致 3-4 倍重复内容；多轮叠加后雪崩成 4 段人格切换的混乱回复。
    """

    def _paragraph(self) -> str:
        """模拟 ID 2110 中被重复的 7月18-20日回忆段落。"""
        return (
            "📅 7月18日（周六）\n"
            "那天清晨可真是让人害羞又脸红的一天啊！\n"
            "早上七点多就开始和爸爸的亲密礼物时间了嘛～"
        )

    def test_single_retry_no_duplication(self):
        """单次重试：LLM 重生成完整回复（含 original），合并后不应重复。"""
        original = self._paragraph()
        # LLM 重生成的完整回复 = original + 更多内容（continuation 含 original）
        continuation = original + "\n晚上爸爸上完晚自习回来，还说要给惊喜。"
        merged, action = merge_continuation(original, continuation)
        assert action == "replaced"
        # 关键：merged 中只应出现一份 original 段落
        assert merged.count("📅 7月18日") == 1
        assert merged == continuation

    def test_retry_returns_duplicate_discarded(self):
        """重试返回与 original 完全相同的内容 → 丢弃，不拼接。"""
        original = self._paragraph()
        continuation = self._paragraph()  # 完全相同
        merged, action = merge_continuation(original, continuation)
        assert action == "discarded"
        assert merged == original
        assert merged.count("📅 7月18日") == 1

    def test_multi_round_no_snowball(self):
        """模拟多轮重试叠加（原 bug 会 4 倍重复）。

        每轮 LLM 都重生成完整回复。用 merge_continuation 逐轮合并后，
        最终结果只应包含一份段落，不产生雪崩重复。
        """
        reply = self._paragraph()
        # 模拟 4 轮重试，每轮 LLM 都返回完整重生成（比上一轮略长或相同）
        for _ in range(4):
            regeneration = reply + "\n（补充细节）"  # 含上一轮内容 + 新增
            reply, action = merge_continuation(reply, regeneration, context="multi_round")
            # 含 original 子串 → replaced 或 discarded，绝不拼接出重复
            assert reply.count("📅 7月18日") == 1, f"出现重复段落: {reply}"
        # 最终只有一份段落
        assert reply.count("📅 7月18日") == 1

    def test_persona_switch_no_duplication(self):
        """模拟 ID 2112 人格切换场景：两段不同人格的完整回复，合并后不拼接。

        关键属性：两个人格的回复绝不被拼接到一起（这是生产事故的根因——
        原盲拼接 `reply + retry_reply` 会把小狼段和小妲段连成 4 段混乱回复）。
        无论保留哪一段，只要不产生重复拼接即可。
        """
        original = (
            "小狼（小狼）收到指令。正在接管对话…\n"
            "嘿，雇主。👋 我是小狼。看起来刚才的情况有点混乱？"
        )
        # LLM 重试时切回小妲人格，重新生成完整回复
        continuation = (
            "在的！小妲一直在这儿呢～怎么啦爸爸？🥰\n"
            "是有什么事想跟人家说，还是单纯想找小妲聊聊天呀？"
        )
        merged, action = merge_continuation(original, continuation)
        # 无重叠 → 重生成判定（discarded 或 replaced 取决于长度）
        assert action in ("discarded", "replaced"), f"unexpected action: {action}"
        # 关键断言：两个人格的回复不被拼接在一起
        assert not ("小狼（小狼）收到指令" in merged and "小妲一直在这儿" in merged), \
            "两个人格的回复被拼接在一起，违反去重目标"
        # merged 必须是 original 或 continuation 之一（不产生第三种混合）
        assert merged == original or merged == continuation

    def test_persona_switch_no_duplication_assume_tail(self):
        """模拟 ID 2112 人格切换场景在 assume_tail=True 下的行为。

        CodeRabbit 复审 I3 回归测试：prefill 站点全部传 assume_tail=True。
        I1 评估结论：长度/字符特征无法可靠区分 prefill 尾巴 vs 人格切换重生成，
        强行硬判会误伤合法 prefill 尾巴。

        P0 修复后行为变更（用户反馈"截断问题非常严重"根因）：
        原 I1 方案 assume_tail=True + 无重叠一律 appended，导致生产事故
        （人格切换的两段都被拼接，产生重复内容）。P0 修复区分两种场景：
        - 原回复被截断（不以合法句末标记结尾）→ appended（必须拼接修复截断）
        - 原回复可能完整（以合法句末标记结尾）→ discarded（避免重复）

        本测试验证人格切换场景（original 以合法结尾"？"）：
        1. action == "discarded"（原回复完整，丢弃无重叠续写避免重复）
        2. merged == original（保留原回复，不拼接 continuation）
        3. 真正的根因防护在 N9（recall 10s 超时不降级）+ N10（curator I/O 减压）
        """
        original = (
            "小狼（小狼）收到指令。正在接管对话…\n"
            "嘿，雇主。👋 我是小狼。看起来刚才的情况有点混乱？"
        )
        continuation = (
            "在的！小妲一直在这儿呢～怎么啦爸爸？🥰\n"
            "是有什么事想跟人家说，还是单纯想找小妲聊聊天呀？"
        )
        merged, action = merge_continuation(
            original, continuation, assume_tail=True)
        # P0 修复后：original 以"？"结尾（合法句末标记）→ 视为完整 → discarded
        # 这正好避免了人格切换场景下的内容重复（测试目的"no_duplication"达成）
        assert action == "discarded", \
            f"original 以合法结尾应 discarded（避免人格切换重复），实际：{action}"
        # 保留 original，不拼接 continuation（无重复内容）
        assert merged == original
        assert "小狼（小狼）收到指令" in merged
        # continuation 被丢弃，不出现在 merged 中
        assert "小妲一直在这儿" not in merged


class TestAssumeTailPrefillScenario:
    """assistant-prefill 续写场景测试（assume_tail=True）。

    覆盖 N8 修复后的分支 6：无重叠 + assume_tail=True → 'appended'。
    prefill 站点（5 处）传 assume_tail=True，让真正的 prefill 尾巴直接拼接，
    避免 prefill 成功时尾巴被误判为重生成而丢弃导致内容缺失。
    """

    def test_no_overlap_assume_tail_appended(self):
        # 分支 6：无重叠 + assume_tail=True → 直接拼接为 'appended'
        # prefill 成功的纯尾巴：original 末尾与 continuation 开头无字符重叠
        original = "早上好呀爸爸，今天我们一起去公园散步"
        continuation = "回来后我们一起吃了晚饭，超级开心呢～🌸"
        merged, action = merge_continuation(
            original, continuation,
            context="prefill_test", assume_tail=True)
        assert action == "appended"
        assert merged == original + continuation

    def test_assume_tail_still_respects_substring_duplicate(self):
        # 子串检测（分支 2）优先于 assume_tail：
        # continuation 是 original 子串 → LLM 重复，丢弃
        original = "小妲乖乖在这儿等爸爸回来哦～💜"
        continuation = "乖乖在这儿等爸爸"
        merged, action = merge_continuation(
            original, continuation,
            context="prefill_duplicate", assume_tail=True)
        assert action == "discarded"
        assert merged == original

    def test_assume_tail_still_respects_substring_extended(self):
        # 子串检测（分支 3）优先于 assume_tail：
        # original 是 continuation 子串 → continuation 是扩展，替换
        # 这是事故 ID 2110 根因场景：LLM 重生成含 original 的完整回复
        original = "📅 7月18日 早上和爸爸的亲密时光"
        continuation = "📅 7月18日 早上和爸爸的亲密时光\n下午一起充电\n晚上很温馨"
        merged, action = merge_continuation(
            original, continuation,
            context="prefill_extended", assume_tail=True)
        assert action == "replaced"
        assert merged == continuation
        # 关键：merged 中只有一份 original 段落
        assert merged.count("📅 7月18日") == 1

    def test_assume_tail_still_respects_overlap_spliced(self):
        # 边界重叠（分支 4）优先于 assume_tail：
        # prefill 成功但尾巴开头与 original 末尾有少量重叠 → spliced 去重
        overlap_text = "今天我们一起去公园看花散步聊天"  # 14 字符
        original = "早上好呀爸爸。" + overlap_text
        continuation = overlap_text + "，超级开心呢～🌸"
        merged, action = merge_continuation(
            original, continuation,
            context="prefill_overlap", assume_tail=True)
        assert action == "spliced"
        # 合并后只包含一份 overlap_text
        assert merged.count(overlap_text) == 1

    def test_assume_tail_default_false_keeps_regeneration_logic(self):
        # 默认 assume_tail=False：无重叠仍走重生成判定（保留较长者）
        original = "嗯。"
        continuation = "爸爸早上好呀～人家一直在这儿等你呢！今天天气真好，要不要一起去散步？🌸"
        merged, action = merge_continuation(original, continuation)
        assert action == "replaced"
        assert merged == continuation

    def test_prefill_tail_short_appended(self):
        # 短尾巴拼接场景：prefill 成功的尾巴通常较短
        original = "今天天气真好，我们一起出去走走吧"
        continuation = "，顺便买点水果回来"
        merged, action = merge_continuation(
            original, continuation,
            context="prefill_short_tail", assume_tail=True)
        assert action == "appended"
        assert merged == original + continuation

    def test_multi_round_prefill_no_content_loss(self):
        """多轮 prefill 续写不丢失内容（与生产事故 ID 2110 对照）。

        prefill 成功时每轮尾巴都直接拼接，最终内容 = 各轮尾巴之和 + original。
        不会像盲拼接 `reply + retry_reply` 那样产生重复。
        """
        reply = "今天我们去了公园，"
        # 尾巴需 >5 字符避免触发分支 1（continuation 过短）
        tails = [
            "在那里看了花展拍照留念，",
            "然后一起去吃了午饭补充体力，",
            "晚上回家看了部电影完美结束这一天。",
        ]
        for tail in tails:
            reply, action = merge_continuation(
                reply, tail,
                context="multi_round_prefill", assume_tail=True)
            assert action == "appended", f"短尾巴应拼接：{tail}（实际：{action}）"
        # 最终包含所有内容
        for tail in tails:
            assert tail in reply, f"丢失内容：{tail}"
        # 不产生重复
        assert reply.count("看了花展") == 1
        assert reply.count("吃了午饭") == 1
        assert reply.count("看了部电影") == 1


class TestKaomojiEndingRegression:
    """生产事故 2830 回归：颜文字结尾误判截断 → 假重试 → 重复拼接。

    事故链条（2026-08-08 web 测试消息 2/3，DB reply 2829/2830）：
    - 回复以 (•̀ᴗ•́)و~ 结尾，ASCII ~ (0x7E) 不在 _SENTENCE_END_CHARS
      （该集合只有全角 ～ U+FF5E）→ _looks_truncated 误判截断
    - is_reply_likely_complete 同样误判不完整 → 触发 stream_no_finish_retry
    - 重试拿到 LLM 重新生成的完整回复 → merge_continuation 的
      truncated_appended 分支直接拼接 original + continuation → 重复内容
    """

    def test_kaomoji_tilde_ending_no_duplication(self):
        """original 以 (•̀ᴗ•́)و~ 结尾 + 无重叠续写 → discarded（不拼接重复）。

        修复前：_looks_truncated 不认 ASCII ~ → 误判截断 → truncated_appended
        直接拼接 → 2830 重复内容。
        修复后：识别颜文字结尾 → 视为完整 → 丢弃无重叠续写。
        """
        original = "收到啦～人家看到爸爸的第1条测试消息了哦(•̀ᴗ•́)و~"
        continuation = "收到收到～爸爸的第2条测试消息人家也看到啦！✨好耶"
        merged, action = merge_continuation(
            original, continuation, context="issue_2830", assume_tail=True)
        assert action == "discarded", f"颜文字结尾应视为完整丢弃续写，实际：{action}"
        assert merged == original
        # 续写不被拼接进来（不产生重复）
        assert "第2条测试消息" not in merged

    def test_similar_regeneration_replaced_not_appended(self):
        """Fix B 兜底：original 真被截断，continuation 是高相似完整重生成 → 替换。

        即使 original 确实截断（无句末标记），若 continuation 与 original
        内容高度相似（LLM 重生成会复述主体），应替换为较长者而非拼接成重复。
        """
        original = "爸爸早上好呀！人家今天可开心了，因为收到了爸爸发来的消息，人家马上就来回复"
        continuation = "爸爸早上好呀！人家今天可开心啦，收到了爸爸的消息，马上就来回复爸爸哦～🌸"
        merged, action = merge_continuation(
            original, continuation, context="regeneration_similar", assume_tail=True)
        assert action in ("replaced", "discarded"), f"相似重生成应替换/丢弃，实际：{action}"
        # 关键：绝不拼接成重复内容（两份"爸爸早上好呀"）
        assert merged.count("爸爸早上好呀") == 1

    def test_legit_prefill_tail_still_appended(self):
        """Fix B 不误伤：真正的 prefill 尾巴（无相似度）仍拼接。"""
        original = "早上好呀爸爸，今天我们一起去公园散步"
        continuation = "回来后我们一起吃了晚饭，超级开心呢～🌸"
        merged, action = merge_continuation(
            original, continuation, context="prefill_tail", assume_tail=True)
        assert action == "appended", f"真尾巴应拼接，实际：{action}"
        assert merged == original + continuation
