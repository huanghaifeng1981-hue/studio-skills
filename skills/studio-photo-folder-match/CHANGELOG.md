# studio-photo-folder-match 更新记录

## 26.11.3（2026-11-24 · 统一命名并迁入 studio-skills 新仓）

- 技能 ID 由 `photo-folder-match` 统一规范更名为 `studio-photo-folder-match`，状态明确为 `active`（macOS 真实排控表查找与 SMB 防覆盖复制已验证）。
- 剥离原 `SKILL.md` 中硬编码的私人 NAS 地址与仓库内 `work/` 状态路径，统一改存至仓库外 `StudioData/config/` 与 `StudioData/outputs/`；移除重复分发的 `references/model-policy.md` 副本。
- 新增 `tests/test_copy_photos.py` 自动化单元测试，覆盖精确姓名切分（防子串误匹配）、防路径穿越、防覆盖、断点续传及汇总报告。

## 26.11.2 及以前（旧仓历史摘要）

- 建立选片表与摄影表字段区分、同名多目录全列及 SMB 排他创建＋所有权日志安全复制脚本 `copy_photos.py`。
