# studio-audio-notes 更新记录

## 26.11.16（2026-11-24 · 清洗迁入 studio-skills 新仓并与 StudioData 解耦）

- 迁入 `studio-skills` V2 仓库，修正标题残留的版本后缀（统一为「录音整理助手」），清理正文中所有旧目录名 `local/customer-audio-notes/` 引用，统一对齐至仓库外 `StudioData/models/`、`StudioData/references/studio-audio-notes/` 与 `StudioData/outputs/`。
- 移除重复分发的 `references/model-policy.md`，新增 `tests/test_record_correction.py` 单元测试校验纠错记录追加与时间区间防呆。

## 26.11.15 及以前（旧仓历史摘要）

- 由 `customer-audio-notes` 更名为 `studio-audio-notes`，支持 PyAV 内存解码 m4a/WAV、MOSS 离线识别、三类业务场景摘要及选片双文档自动归档。
