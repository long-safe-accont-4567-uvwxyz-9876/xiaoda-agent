import asyncio
import os
try:
    import streamlit as st
except ImportError:
    raise ImportError("streamlit 未安装，请运行: pip install streamlit") from None
from agent_core import AgentCore

st.set_page_config(
    page_title="AI Agent",
    page_icon="🤖",
    layout="wide"
)

# === 密码认证 ===
WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if WEBUI_PASSWORD:
    if not st.session_state.authenticated:
        st.title("🤖 AI Agent")
        st.markdown("---")
        pwd = st.text_input("请输入访问密码", type="password", key="login_pwd")
        if st.button("登录"):
            if pwd == WEBUI_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("密码错误，请重试")
        st.stop()
else:
    st.warning("⚠️ 未设置 WEBUI_PASSWORD 环境变量，任何人都可以访问此界面。建议设置密码以保护安全。")

st.title("🤖 全能型 AI Agent")
st.caption("智能助手 | 系统操作 • 文件管理 • 网络搜索 • 代码执行")

if "agent" not in st.session_state:
    with st.spinner("正在初始化 AI Agent..."):
        agent = AgentCore()
        loop = asyncio.new_event_loop()
        loop.run_until_complete(agent.init())
        st.session_state.agent = agent
        st.session_state._loop = loop
    st.success("✅ AI Agent 已就绪！")

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.header("🛠️ 工具列表")
    st.markdown("""
    **📁 文件操作**
    - 列出目录内容
    - 读取/写入文件
    - 搜索文件
    - 执行 Shell 命令

    **💻 代码执行**
    - Python 代码运行
    - 数学计算

    **🌐 网络工具**
    - 网络搜索
    - 天气查询
    """)

    st.header("💡 使用示例")
    st.markdown("""
    - "列出主目录的文件"
    - "读取某个配置文件"
    - "搜索 Python 教程"
    - "计算 123 * 456"
    - "执行代码：打印九九乘法表"
    - "北京今天天气怎么样"
    """)

    if st.button("清空对话"):
        st.session_state.messages = []
        st.rerun()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.text(message["content"])
        else:
            st.markdown(message["content"], unsafe_allow_html=False)

if prompt := st.chat_input("输入你的问题..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.text(prompt)

    with st.chat_message("assistant"), st.spinner("思考中..."):
        result = st.session_state._loop.run_until_complete(st.session_state.agent.process(prompt))
        st.markdown(result.reply, unsafe_allow_html=False)
        if result.image_paths:
            for img_path in result.image_paths:
                st.image(str(img_path))
        if result.audio_path:
            st.audio(str(result.audio_path))
        if result.sticker_path:
            st.image(str(result.sticker_path))

    st.session_state.messages.append({"role": "assistant", "content": result.reply})
