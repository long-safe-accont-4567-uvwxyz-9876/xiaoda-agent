# xiaoda-agent 文档索引

> 本文档统一索引项目所有文档，按角色分类。最后更新: 2026-07-19
>
> 链接路径均相对于本文件（`docs/INDEX.md`）。标注"（内部使用）"的文档主要面向项目维护者与 AI 协作工具，普通使用者无需阅读。

## 🚀 快速开始

- [项目概述](../README.md) - 项目介绍、核心特色、能力对比
- [安装部署](../SETUP.md) - Docker / 手动 / systemd 三种部署方式
- [使用指南](../USAGE.md) - CLI 命令、QQ Bot、Web UI 使用说明
- [环境变量参考](../.env.example) - 完整环境变量配置模板（含字段注释）

## 👨‍💻 开发者

- [架构设计](ARCHITECTURE.md) - 系统架构、模块划分、调用链
- [API 文档](API.md) - Web API 接口规范
- [CLAUDE 开发指南](../CLAUDE.md)（内部使用） - 开发命令、架构说明、配置体系
- [改进计划](IMPROVEMENT_PLAN.md) - 项目改进路线图
- [WebUI 设计](WEBUI_DESIGN.md) - Web 前端设计文档
- [场景感知重设计](SCENE_AWARE_REDESIGN.md) - 场景感知系统重设计

## 🔧 部署运维

- [Docker 部署](../SETUP.md) - Docker Compose 一键部署（推荐）
- [docker-compose.yml](../docker-compose.yml) - 基础 Compose 配置
- [docker-compose.dev.yml](../docker-compose.dev.yml) - 开发环境配置
- [docker-compose.prod.yml](../docker-compose.prod.yml) - 生产环境配置
- [Dockerfile](../Dockerfile) - 容器镜像构建文件
- [健康检查脚本 (Linux)](../scripts/doctor.sh) - 自检脚本
- [健康检查脚本 (Windows)](../scripts/doctor.bat) - 自检脚本
- [K8s 部署配置](../deploy/k8s.yaml) - Kubernetes 部署
- [systemd 服务单元](../deploy/qq-agent.service) - QQ Bot systemd 服务

## 📊 审计与质量

- [代码质量审计报告](../audit/code_quality_audit_report.md) - 代码质量评估
- [代码质量审计证据](../audit/code_quality_audit_evidence.md) - 审计原始证据
- [渠道审计报告](../audit/channel_audit_report.md) - 交互通道审计
- [渠道审计证据](../audit/channel_audit_evidence.md) - 渠道审计证据
- [Agent 系统掌握度规格](../audit/agent-system-mastery-spec.md) - 系统能力规格
- [缺陷扫描报告](../.scan_reports/defect_scan_report.md) - 缺陷扫描结果
- [缺陷扫描证据](../.scan_reports/defect_scan_evidence.md) - 扫描原始证据
- [v0.4.96 评审报告](../.scan_reports/v0.4.96_review_report.md) - 版本评审
- [v0.4.95 审计报告](../.scan_reports/v0.4.95_audit_report.md) - 版本审计
- [v0.4.83 评审报告](../.scan_reports/v0.4.83_review_report.md) - 版本评审
- [FSRS/DSR 审计报告](../audit_fsrs_dsr_report.md) - 记忆系统审计
- [J-Space 审计报告](../audit_j_space_report.md) - J-Space 系统审计
- [Docker 检查报告](../docker-inspection-report.md) - Docker 部署检查
- [Linux 安装包检查报告](../Linux安装包检查报告.md) - Linux 打包检查
- [质量评分规格](quality_score_spec.md) - 质量评分体系规格
- [修复规格 P0](fix_spec_P0.md) - P0 优先级修复规格
- [修复规格 P1](fix_spec_P1.md) - P1 优先级修复规格
- [修复规格 P2](fix_spec_P2.md) - P2 优先级修复规格

## 🧪 测试

- [测试目录](../tests/) - 单元测试与集成测试
- [CI 测试工作流](../.github/workflows/ci-tests.yml) - CI 测试配置
- [构建发布工作流](../.github/workflows/build-release.yml) - 构建发布配置

## 📐 设计规格

- [Spec 文档目录](../.trae/specs/) - Trae 规格文档
- [报告问题批量修复 Spec](../.trae/specs/report-issues-batch-fix/spec.md) - 当前批次修复规格
- [docs/SPECS 子规格](SPECS/) - 文档子规格目录
- [项目规格目录](../specs/) - 项目规格文档
- [RAG 优化规格](../RAG-OPTIMIZATION-SPEC.md) - RAG 检索优化规格
- [改进计划](IMPROVEMENT_PLAN.md) - 项目改进路线图
- [Bugfix 计划 2026-07-19](../BUGFIX_PLAN_2026-07-19.md) - 最新 Bugfix 计划
- [优化任务清单](../optimization-tasks.md) - 优化任务清单
- [项目记忆](../MEMORY.md)（内部使用） - 项目记忆文档

## 📌 内部使用文档

以下文档主要供项目维护者与 AI 协作工具使用，普通使用者无需阅读：

- [CLAUDE.md](../CLAUDE.md) - Claude Code 协作指南
- [MEMORY.md](../MEMORY.md) - 项目记忆
- [BUGFIX_PLAN_2026-07-19.md](../BUGFIX_PLAN_2026-07-19.md) - Bugfix 计划
- [优化任务清单](../optimization-tasks.md) - 优化任务
- [QQ 问候泄漏调试](../debug-qq-greeting-leak.md) - QQ 调试记录
- [QQ 流式调试证据](../debug-qq-streaming-evidence.md) - QQ 流式调试
- [评测报告](../evaluation-report.md) - 系统评测报告
- [掘金文章草稿](juejin-article.md) - 掘金技术文章
