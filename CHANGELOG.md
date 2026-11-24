# 仓库里程碑记录

> 本文件仅记录仓库架构变更及各技能迁入、正式启用（`active`）等重要节点；各技能内部的日常细节迭代记录在 `skills/<skill-id>/CHANGELOG.md`。

## 2026-11-24 · 迁入飞书知识库同步技能

- 审核清洗并迁入第 1 个技能 `studio-feishu-wiki-sync@26.11.5`（`active`）：串联包内 `lark-cli` 参考手册、移除旧文件命名假设、补齐本地相对链接与 `feishu_url` 双向转换及回读验收规则。

## 2026-11-24 · 初始化干净新仓架构

- 建立 `studio-skills` 极简代码仓与仓库外 `StudioData/` 本机数据目录，实现代码与大模型、样片、运行产物彻底分家。
- 确立单文件助手入口 `AGENTS.md`、四段式技能模板及支持单技能精准部署的 `scripts/install.py`。
