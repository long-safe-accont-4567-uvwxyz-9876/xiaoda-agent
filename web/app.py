import asyncio
import streamlit as st
from agent_core import AgentCore

st.set_page_config(
    page_title="AI Agent",
    page_icon="🤖",
    layout="wide"
)

st.title("🤖 全能型 AI Agent")
st.caption("运行在 Orange Pi 上的智能助手 | 系统操作 • 文件管理 • 网络搜索 • 代码执行")

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
        st.markdown(message["content"])

if prompt := st.chat_input("输入你的问题..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    
    with st.chat_message("assistant"):
        with st.spinner("思考中..."):
            response = st.session_state._loop.run_until_complete(st.session_state.agent.process_text(prompt))
            st.markdown(response)
    
    st.session_state.messages.append({"role": "assistant", "content": response})