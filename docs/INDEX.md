# xiaoda-agent 文档索引

> 本文档统一索引项目所有文档，按角色分类。最后更新: 2026-10-28
>
> 链接路径均相对于本文件（`docs/INDEX.md`）。标注"（内部使用）"的文档主要面向项目维护者与 AI 协作工具，普通使用者无需阅读。
>
> **维护约定**：新增或移动文档时同步更新本索引；已删除的历史文档不再保留链接，避免出现死链。

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
- [废弃模块清单](DEPRECATED_MODULES.md)（内部使用） - 已弃用模块与替代方案
- [本地 AI 平台](local-ai-platform.md) - 本地模型接入说明
- [情感陪伴升级方案](research/external-specs/astrbot-tdbm-companion-upgrade.md) - 基于 AstrBot / TencentDB-Agent-Memory 的对照研究与 P0-P2 优化路线（2026-08-23）
- [情感陪伴升级核验记录](research/external-specs/astrbot-tdbm-companion-upgrade-validation.md) - 外部固定点、已证伪假设、实施与灰度门禁（2026-08-23）
- [检索、RAG 与功能节点提示词优化调研](research/retrieval-rag-functional-node-prompt-optimization-2026-08-23.md) - 当前代码基线、16 节点提示词治理、评测闭环与 P0-P4 路线（2026-08-23）

## 🔧 部署运维

- [Docker 部署](../SETUP.md) - Docker Compose 一键部署（推荐）
- [docker-compose.yml](../docker-compose.yml) - 基础 Compose 配置
- [docker-compose.dev.yml](../docker-compose.dev.yml) - 开发环境配置
- [docker-compose.prod.yml](../docker-compose.prod.yml) - 生产环境配置
- [Dockerfile](../Dockerfile) - 容器镜像构建文件
- [健康检查脚本 (Linux)](../scripts/doctor.sh) - 自检脚本
- [健康检查脚本 (Windows)](../scripts/doctor.bat) - 自检脚本
- [K8s 部署配置](../deploy/k8s.yaml) - Kubernetes 部署（参考模板，未随版本验证）
- [NPU 部署 Runbook](NPU_NBG_SEQ128_RUNBOOK.md) - NBG 模型转换与板端部署
- [NPU 重建计划](npu_nbg_rebuild_seq128_plan.md) - seq128 模型重建方案
- [向量库数据路径修复](vector_db_data_path_fix.md) - 数据目录迁移说明

## 🧪 测试

- [测试目录](../tests/) - 单元测试与集成测试
- [CI 测试工作流](../.github/workflows/ci-tests.yml) - CI 测试配置
- [构建发布工作流](../.github/workflows/build-release.yml) - 构建发布配置

## 📐 设计规格

- [docs/SPECS 子规格](SPECS/) - 文档子规格目录
- [项目规格目录](../specs/) - 项目规格文档
- [质量评分规格](quality_score_spec.md) - 质量评分体系规格
- [修复规格 P0](fix_spec_P0.md) - P0 优先级修复规格
- [修复规格 P1](fix_spec_P1.md) - P1 优先级修复规格
- [修复规格 P2](fix_spec_P2.md) - P2 优先级修复规格

### Workflow V2

- [设计原则](workflow-v2-principles.md) - V2 工作流核心设计约束
- [收敛清单](workflow-v2-convergence-checklist.md) - 落地检查项
- [上线推广计划](workflow-v2-promotion-plan.md) - 灰度与回滚方案
- [评审记录](workflow-v2-review.md) - 评审结论与遗留项

## 📊 审计与质量（内部使用）

### 缺陷与评审扫描

- [缺陷扫描报告](../.scan_reports/defect_scan_report.md) - 缺陷扫描结果
- [缺陷扫描证据](../.scan_reports/defect_scan_evidence.md) - 扫描原始证据
- [v0.4.96 评审报告](../.scan_reports/v0.4.96_review_report.md) - 版本评审
- [v0.4.95 审计报告](../.scan_reports/v0.4.95_audit_report.md) - 版本审计
- [v0.4.83 评审报告](../.scan_reports/v0.4.83_review_report.md) - 版本评审

### 专项审计

- [技术债审计 2026-08-25](tech_debt_audit_2026-08-25.md) - 最新技术债盘点
- [技术债审计 2026-08-22](tech_debt_audit_2026-08-22.md) - 上一轮技术债盘点
- [评审台账 2026-08-25](review_ledger_2026-08-25.md) - 问题闭环台账
- [宽泛 except 清单](broad_except_inventory_2026-08-22.md) - 异常捕获收敛依据
- [核心孪生模块审计](core_twin_modules_audit_2026-08-25.md) - 重复实现排查
- [巨型文件拆分计划](giant_files_split_plan_2026-08-22.md) - 大文件治理方案
- [性能审计 2026-07-20](performance_audit_2026-07-20.md) - 性能基线
- [Go 优化机会扫描](GO_OPTIMIZATION_OPPORTUNITIES.md) - 可迁移到 Go 的热点
- [仓库卫生笔记](repo_hygiene_notes.md) - 目录与产物清理约定
- [hnswlib 512 维召回失败分析](hnswlib-512d-recall-failure.md) - 向量检索故障复盘
- [KGraph 双渲染](kgraph-dual-render.md) - 知识图谱双路径渲染
- [Rust 混合方案 PoC](rust-hybrid-poc.md) - Rust 加速可行性验证

## 📌 内部使用文档

以下文档主要供项目维护者与 AI 协作工具使用，普通使用者无需阅读：

- [CLAUDE.md](../CLAUDE.md) - Claude Code 协作指南
- [MEMORY.md](../MEMORY.md) - 项目记忆
- [TODO.md](TODO.md) - 待办事项
- [掘金文章草稿](juejin-article.md) - 掘金技术文章
