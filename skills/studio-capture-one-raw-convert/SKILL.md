---
name: studio-capture-one-raw-convert
description: 接收 AI 选片结果，在 macOS 上通过 Capture One（飞思）原生 AppleScript 接口对选中 RAW 逐张完成以脸部曝光为基准的分区校正、默认基础人像润饰及原始尺寸 sRGB 100 质量 JPG 转档交接。
metadata:
  version: "26.11.14"
  status: draft
  title: 飞思 AI 转档
---

# 飞思 AI 转档

## 1. 用途与交付

- **适用场景**：对应自动出片链路中的「AI 选片 → 飞思 RAW 转档 → 下游精修诊断」环节。仅处理选片记录中明确选中的 RAW 照片（追色与精细液化修图由下游负责）。
- **必要输入**：选片记录（照片 ID、原始 RAW 路径、选中状态）＋ 已确认的转档参考样片库路径。
- **交付产物**：
  - 每张通过检查的照片输出一份**原始完整画面（不裁剪、不缩放）、sRGB、质量 100 的 JPG 中间图**（输出至仓库外 `StudioData/outputs/studio-capture-one-raw-convert/` 或指定交接目录）；
  - 随附下游精修所需的[交接记录](references/handoff.md)（含实际应用的调整项、润饰状态与待复核标记）。

## 2. 环境、配置与边界

- **支持平台**：当前仅支持 **macOS + Capture One Pro**（通过 `osascript` 调用飞思原生 AppleScript 字典 + `/usr/bin/sips` 亮度统计，详见 [references/capture-one.md](references/capture-one.md)；Windows 尚未适配，状态保持 `draft`）。
- **接口与原片保护红线**：
  - **严禁使用电脑屏幕点击、System Events 界面脚本或快捷键代替飞思 AppleScript 原生接口**。
  - RAW 原片保留在原路径不动，已有人工调整时在克隆变体上操作；已有裁切在转档克隆变体中恢复完整画面。
  - **禁止同组批量套用或复制基础曝光/白平衡数值**：同场景可共用视觉参考图，但每张照片必须由视觉模型看当前渲染预览独立决策并重新渲染核验。
- **外部参考库位置**：工作站正式库说明见 `StudioData/config/studio-capture-one-workstation.md`，本机参考样片库存放于 `StudioData/references/米乐转档参考库/`（索引与同名备注规范见 [索引模板](references/reference-index-template.md)、[备注模板](references/reference-note-template.md)）。

## 3. 执行步骤

1. **盘点变体与匹配参考片**：
   - 在飞思专用集合中核对选中照片 ID、RAW 路径与导出配方。
   - 导出低分辨率基线预览，按照脸光源、脸部朝向、受光面与光比从参考库 `索引.md` 选取 1–3 张相近布光样片（若遇无参考新场景，先做 1–2 张代表片交负责转档同事确认后再扩批）。
2. **以人物脸部曝光为首要基准，按需分区调整**：
   - 结合 [scripts/preview-metrics.py](scripts/preview-metrics.py) 分位读数与视觉审阅判断主体明暗。
   - 全局偏差先用整体曝光把脸调近目标，局部偏差按需调用飞思四类蒙版（**面部皮肤、身体皮肤、主体、背景**，不含独立天空蒙版，属性见 [capture-one.md#曝光蒙版](references/capture-one.md#曝光蒙版)），辅以白平衡、高动态范围、色阶、曲线或轻量清晰度，避免多项工具对同一区间重复补偿。改后重新渲染复查额头/白衣高光与暗部层次。
3. **统一应用默认基础人像润饰**：
   - 全部选中照片统一复制应用已确认的米乐默认润饰值（覆盖面部皮肤、碎发、牙齿、双眼、身体皮肤与肤色统一，不做“皮肤好不好需不需要润饰”的前置分支判断，详见 [capture-one.md#皮肤润饰](references/capture-one.md#皮肤润饰)），复制前后读回核对参数并检查五官边缘自然度。
4. **导出完整尺寸中间图**：
   - 仅将质检通过的转档变体按 `JPEG / 质量 100 / sRGB / 原始完整尺寸` 配方导出，无法可靠校正的变体保留最佳状态并标为`待人工复核`。

## 4. 验收与异常恢复

- **完成标准**：未选中照片零改动且未混入输出、RAW 原片完好、导出的 JPG 可正常解码且像素尺寸与原始 RAW 完整画面一致、色彩空间为 sRGB、交接记录一对一完整。
- **中断恢复**：重跑前先核对已导出的 JPG、飞思集合内已有转档变体及交接记录，只补做未完成照片，不重复创建克隆变体或覆盖已验收中间图。
