"""strip_image_gen_leak 测试：清洗 LLM 伪造图片生成时泄漏的模型名/状态行/生图元数据。

生产样本 conversation_logs id 1965/1966：
- "Agnes Image 2.1 Flash 刚才跟我撒娇..."
- "【图片生成中 —— Agnes Image 2.1 Flash ⚡】"
- 'Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "..."'
"""
from utils.llm_cleanup import strip_image_gen_leak


def test_strips_model_name_with_trailing_space():
    text = "Agnes Image 2.1 Flash 刚才跟我撒娇说需要更多星光"
    assert strip_image_gen_leak(text) == "刚才跟我撒娇说需要更多星光"


def test_strips_model_name_mid_sentence():
    text = "是 Agnes Image 2.1 Flash 在帮忙啦"
    assert strip_image_gen_leak(text) == "是 在帮忙啦"


def test_strips_video_model_name():
    text = "用 Agnes Video V2.0 做的"
    assert strip_image_gen_leak(text) == "用 做的"


def test_strips_model_id_form():
    assert strip_image_gen_leak("模型 agnes-image-2.1-flash 已就绪") == "模型 已就绪"


def test_strips_fabricated_status_line():
    text = "好的\n【图片生成中 —— Agnes Image 2.1 Flash ⚡】\n给你"
    out = strip_image_gen_leak(text)
    assert "【图片生成中" not in out
    assert "好的" in out and "给你" in out


def test_strips_fabricated_metadata_line():
    text = '图来了\nWidth Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "cat"\n收好'
    out = strip_image_gen_leak(text)
    assert "Width Height" not in out
    assert "Seed: 93847" not in out
    assert "图来了" in out and "收好" in out


def test_does_not_strip_normal_model_mention():
    # 仅含 "Model: GPT" 的普通行不在完整序列里，不应删
    text = "这个 Model: GPT 的回复不错"
    assert strip_image_gen_leak(text) == "这个 Model: GPT 的回复不错"


def test_production_sample_1966_fragment_clean():
    fragment = (
        "Agnes Image 2.1 Flash 刚才跟我撒娇说需要更多星光才能点亮细节…"
        "不过——你看！终于准备好送给主人了 !! "
        'Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| '
        'Prompt: "A super cute kawaii anime self portrait"'
    )
    out = strip_image_gen_leak(fragment)
    assert "Agnes Image 2.1 Flash" not in out
    assert "Width Height" not in out
    assert "Seed: 93847" not in out
    assert "Prompt:" not in out


def test_strips_pollinations_url_params_residue():
    # 异常 markdown ![alt](url)?width=...&nologo=true) 留下的参数尾部残留
    text = "图来了\n?width=560&height=792&seed=48392&nologo=true)\n呼哧"
    out = strip_image_gen_leak(text)
    assert "?width=" not in out
    assert "nologo=true" not in out
    assert "图来了" in out and "呼哧" in out


def test_strips_url_params_residue_without_trailing_paren():
    # 参数残留不以 ) 结尾的情况
    text = "?width=560&height=792&seed=48392&nologo=true 收好"
    out = strip_image_gen_leak(text)
    assert "?width=" not in out
    assert "nologo" not in out
    assert "收好" in out


def test_does_not_strip_normal_query_string():
    # 正常 ?key=val 文本不应被删（缺少完整 width+height+seed+nologo 序列）
    text = "访问 https://example.com?width=100 试试"
    assert strip_image_gen_leak(text) == "访问 https://example.com?width=100 试试"


def test_production_sample_1965_url_params_residue_clean():
    # id 1965 的异常 markdown ![alt](url)?width=...true) 残留
    fragment = (
        "“正在为爸爸的专属画框注入灵魂墨水和草莓味星光...” ☂️🎨~\n"
        "?width=560&height=792&seed=48392&nologo=true)\n"
        "*\n"
        "呼哧……看、看懂了吗！！"
    )
    out = strip_image_gen_leak(fragment)
    assert "?width=" not in out
    assert "nologo=true" not in out
    assert "seed=48392" not in out
    assert "灵魂墨水" in out
    assert "呼哧" in out


def test_empty_input():
    assert strip_image_gen_leak("") == ""
    assert strip_image_gen_leak(None) == ""  # type: ignore[arg-type]
