# 🌿 白草净华 — 纳西妲 AI Agent

> *此为净善智慧之主——纳西妲，愿你的梦境如白草般芬芳。*  
> *兰那罗在林间传唱，白草随风摇曳，一切知识皆可触及。*

## 📖 项目简介

纳西妲 AI Agent 是一个运行在 Orange Pi 4 Pro（4GB RAM）上的**多 Agent 智能助手系统**，以小米 **MiMo-V2.5 / V2.5-Pro** 大模型为核心，集成了 QQ Bot、CLI 交互界面和 Web UI，支持工具调用、硬件控制、视觉识别、记忆系统等丰富功能。

## ✨ 核心特性

- 🤖 **多 Agent 协作**：纳西妲（主控）、希兰（搜索）、银狼（代码）、妮可（研究）、可莉（子Agent）
- 🧠 **记忆系统**：短期上下文 + 长期记忆 + 语义向量检索（sqlite-vec + BGE-M3）
- 🔧 **工具调用**：Shell、文件、Python、网络搜索、硬件控制（GPIO/I2C/SPI/PWM/UART）
- 👁️ **视觉识别**：图像理解、截图分析、摄像头捕获
- 🗣️ **TTS 语音合成**：edge-tts / pyttsx3 双引擎
- 📝 **智能笔记本**：AI 自动提取知识、多维检索
- 🏠 **主动关怀**：定时问候、提醒、知识推送
- 🔒 **安全防护**：Prompt注入检测、频率限制、紧急熔断
- 🚀 **NPU 加速**：全志 T507 芯片 NPU 推理
- 🌐 **Web UI**：Flask + Vue3 管理面板

## 🚀 快速开始

```bash
cd nahida-agent
pip install -r requirements.txt
cp .env.example .env
python agent.py
```

## 📄 许可证

MIT License
