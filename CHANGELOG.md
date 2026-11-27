# 仓库里程碑记录

> 本文件仅记录仓库架构变更及各技能迁入、正式启用（`active`）等重要节点；各技能内部的日常细节迭代记录在 `skills/<skill-id>/CHANGELOG.md`。

## 2026-11-27 · 飞思 AI 转档技能完成十一轮审计与实机验收正式启用

- **`studio-capture-one-raw-convert@26.11.28`（`active`）**：完成十一轮高强度白盒安全/崩溃一致性审计（51 项包内自动化回归测试 + 7 项仓库级测试）与 macOS Capture One Pro 16.9 真实客片端到端试跑验收（含 NAS 正式参考库联动、`Studio_Default_Retouch` 自动人像润饰+锐化、AI 面部蒙版分层调参、`--overwrite-own` 无损重导与 `handoff.jsonl` 账本对平），经用户确认正式启用。

## 2026-11-24 · 完成全部 7 个核心 `studio-*` 技能清洗入库

- **外部大体积资产归位**：将 4.1 GB 本地模型、词表、局域网 Ollama 配置及米乐转档参考样片库统一移至仓库外 `StudioData/`，实现代码仓零数据负担。
- **逐一清洗并迁入剩余 5 个技能**：
  - `studio-photo-selection@26.11.7`（`active`）：清理失效的上古项目指纹锁与重复说明，保留 28 项测试与眼神复核/儿童构成规则。
  - `studio-jianying-bts-editor@26.11.9`（原 `jianying-bts-editor`，`active`）：补齐 `studio-` 前缀，彻底铲除失效的上古跨目录 Python 僵尸脚本，保留 7 个 `.mjs` 界面状态机与 6 份测试。
  - `studio-checkin-video@26.11.28`（`active`）：更新下游技能 `studio-*` 新引用名，工作目录收敛至 `StudioData/outputs/`，补齐 `day_queue.py` 单元测试与故障速查卡。
  - `studio-audio-notes@26.11.16`（`draft`）：清理 `customer-audio-notes` 旧路径残留，对齐 `StudioData/` 模型与词表位置，新增纠错日志测试。
  - `studio-capture-one-raw-convert@26.11.14`（`draft`）：关联 `StudioData/` 参考样片库配置，新增 `preview-metrics.py` 亮度分位单元测试。

## 2026-11-24 · 迁入照片文件夹查找技能

- 审核清洗并迁入第 2 个技能 `studio-photo-folder-match@26.11.3`（原 `photo-folder-match`，`active`）：补齐 `studio-` 前缀，剥离私人 NAS 地址至外部 `StudioData/config/`，修复脚本 UTF-8 跨平台读写与正斜杠相对路径，并补齐自动化单元测试。

## 2026-11-24 · 迁入飞书知识库同步技能

- 审核清洗并迁入第 1 个技能 `studio-feishu-wiki-sync@26.11.5`（`active`）：串联包内 `lark-cli` 参考手册、移除旧文件命名假设、补齐本地相对链接与 `feishu_url` 双向转换及回读验收规则。

## 2026-11-24 · 初始化干净新仓架构

- 建立 `studio-skills` 极简代码仓与仓库外 `StudioData/` 本机数据目录，实现代码与大模型、样片、运行产物彻底分家。
- 确立单文件助手入口 `AGENTS.md`、四段式技能模板及支持单技能精准部署的 `scripts/install.py`。
