# studio-checkin-video 更新记录

## 26.11.28（2026-11-24 · 清洗迁入 studio-skills 新仓）

- 迁入 `studio-skills` V2 仓库，将下游协同技能标识统一更新为 `studio-photo-folder-match`、`studio-photo-selection`、`studio-jianying-bts-editor`。
- 默认本机非同步工作目录收敛至 `StudioData/outputs/studio-checkin-video/YYYY年M月D日选片/`，不再污染用户 `~/Documents/` 根目录；`day_queue.py` 初始化时自动创建上级目录并补齐 Python 3.9 注解兼容。
- 新增 `tests/test_day_queue.py` 单元测试（覆盖全表优先匹配/复制调度、已有成片跳步、单客阻塞隔离与恢复）；将旧运行流水账提炼为 `references/troubleshooting.md` 故障速查表，移除重复的 `model-policy.md`。

## 26.11.27 及以前（旧仓历史摘要）

- 完成 ERP 选片表整批驱动队列、修后照片优先提取、25/22 张儿童构成锁定、剪映 22 槽导出、原 NAS 回存、年度 HTML 视频总表及逐单本机副本/草稿自动清理闭环，经用户多批成片确认启用为 `active`。
