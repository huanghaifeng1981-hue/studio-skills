# studio-capture-one-raw-convert 更新记录

## 26.11.14（2026-11-24 · 清洗迁入 studio-skills 新仓并与 StudioData 解耦）

- 迁入 `studio-skills` V2 仓库，将本地参考样片库与工作站配置说明统一归位至仓库外 `StudioData/references/米乐转档参考库/` 与 `StudioData/config/`。
- 移除重复分发的 `references/model-policy.md`，按新仓四段式结构精炼 `SKILL.md`；新增 `tests/test_preview_metrics.py` 单元测试校验 BMP/sips 亮度分位计算。

## 26.11.13 及以前（旧仓历史摘要）

- 确立 macOS Capture One AppleScript 原生接口操作、以人物脸部曝光为基准的四分区蒙版校正、逐张视觉独立决策（禁套基础参数）、全量默认润饰及不裁切不缩放 100 质量 sRGB JPG 交接规范。
