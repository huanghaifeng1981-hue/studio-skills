# studio-jianying-bts-editor 更新记录

## 26.11.9（2026-11-24 · 统一命名、清理死代码并迁入 studio-skills 新仓）

- 技能 ID 由 `jianying-bts-editor` 统一更名为 `studio-jianying-bts-editor`，同步更新 `SKILL.md` 与 `agents/openai.yaml` 调用标识。
- 彻底移除失效的上古跨目录 Python 依赖锁与僵尸脚本（`runtime-dependencies.json`、`model_request.py`、`batch_plan.py`、`LOCAL_DEPLOYMENT.md`、`references/ollama.md`）以及重复的 `README.md`、`recording-workflow.md`、`existing-draft-workflow.md`、`model-policy.md`。
- 完整保留经真实批次验证的 7 个 `.mjs` 界面分段状态机脚本、6 份自动化测试及 5 份核心模板与 UI 避坑参考文档。

## 26.11.8 及以前（旧仓历史摘要）

- 建立 22 槽母版 `batch_workflow.mjs` 分段断点循环、导出与安全清理适配器，实测完成 1080×1920 / 30fps 批量导出与母版保护清理。
