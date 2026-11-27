---
name: studio-capture-one-raw-convert
description: 在 macOS 上通过 Capture One（飞思）原生 AppleScript 接口对客人 RAW 照片逐张完成整图色温/色调、人物曝光与环境物体曝光分区校正、默认基础人像润饰及原始尺寸 sRGB 100 质量 JPG 转档交接。
metadata:
  version: "26.11.28"
  status: active
  title: 飞思 AI 转档
---

# 飞思 AI 转档

## 1. 用途与交付

- **适用场景**：对应自动出片链路中的「客人 RAW 文件夹（或选片名单） → 飞思 AI 转档 → 下游精修诊断」环节。处理文件夹中的客人 RAW 照片（支持可选按选片名单筛选；风格追色与精细液化修图由下游负责）。
- **必要输入**：客人 RAW 照片目录（或当前已打开的飞思集合）＋ 已确认的转档参考样片库路径 ＋ 默认基础人像润饰预设样式名（推荐仅勾选润饰项的飞思 `Style`，或无客片局部图层时的默认润饰源变体 ID）。
- **交付产物**：
  - 每张通过检查的照片输出一份**原始完整画面（不裁剪、不缩放）、sRGB、质量 100 的 JPG 中间图**（输出至仓库外 `StudioData/outputs/studio-capture-one-raw-convert/` 或指定交接目录）；
  - 批次持久化状态账本 `batch-state.json` 与下游精修所需的[交接记录](references/handoff.md) `handoff.jsonl`（含读回参数、润饰状态、每轮快照与防漏片汇总）。

## 2. 环境、配置与边界

- **支持平台**：当前仅支持 **macOS + Capture One Pro 16.9+**（通过确定性桥接脚本 [scripts/c1_bridge.py](scripts/c1_bridge.py) 调用 `osascript` 飞思原生 AppleScript 字典 + `/usr/bin/sips` 像素/ICC 硬门禁校验 + [scripts/preview-metrics.py](scripts/preview-metrics.py) 亮度统计，详见 [references/capture-one.md](references/capture-one.md)；Windows 尚未适配，macOS 已完成真实客片多场景实机验收并启用）。
- **接口与原片保护红线**：
  - **严禁使用电脑屏幕点击、System Events 界面脚本或快捷键代替飞思 AppleScript 原生接口**。
  - **批次账本与源变体防火墙**：任何克隆、调参、正式导出与交接登记必须绑定 `--batch-state <batch-state.json>`；脚本硬拦截对任何 `source_variant_id` 或未登记变体的修改，断点重跑自动复用已有克隆变体而不重复克隆。
  - **保护客片已有局部编辑**：优先使用只包含默认人像润饰项的飞思预设 `--retouch-style`（完整保留客片原有的局部调整图层与曲线）；若原变体已有局部调整图层，脚本硬拦截剪贴板覆盖模式（`--retouch-source-variant-id`）。
  - **禁止同组批量套用基础曝光/白平衡数值**：同场景可共用视觉参考图，但每张照片必须看当前预览独立决策绝对目标值并按物理单位容差读回核验（曝光容差 `0.02 EV`，任何请求字段缺失即判失败）。
- **外部参考库位置**：优先使用工作站配置 `StudioData/config/studio-capture-one-workstation.md` 中登记的 NAS 正式参考库挂载路径（运行时核对已挂载且索引、JPG 与同名备注可读；未挂载时先挂载或按任务显式指定的参考库目录执行；索引与同名备注规范见 [索引模板](references/reference-index-template.md)、[备注模板](references/reference-note-template.md)）。

## 3. 执行步骤

1. **建立批次账本、校验配方与幂等准备克隆变体**：
   - 运行 `python3 scripts/c1_bridge.py init-batch --batch-state <batch-state.json> --raw-dir <RAW目录> [--browse] [--selected-ids ...]` 登记整批期待处理的 RAW 与源变体映射，反向逐项核对 `--selected-ids`（跨扩展名同干名如 `A.CR2` 与 `A.NEF` 自动保留完整文件名区分，绝不相互吞没；缺失项默认报错阻断，或加 `--record-missing-selected` 记入缺源使 `balanced` 持续为 `false`）；运行 `ensure-recipes` 建立并强校验 sRGB 预览配方与正式交接配方。
   - 逐张运行 `python3 scripts/c1_bridge.py prepare-variant --batch-state <batch-state.json> --photo-id <ID> [--retouch-style <润饰样式名>]`：幂等创建或复用克隆变体、快照登记从客片继承的全部既有局部图层（严禁后续改写同名客片层）、恢复完整画面、套入默认基础人像润饰并写回全部 28 项基础滑杆及色彩配置/胶片曲线基线。
2. **导出无元数据预览与匹配参考片**：
   - 运行 `python3 scripts/c1_bridge.py export --mode preview --batch-state <batch-state.json> --variant-id <克隆ID> --recipe AI_Preview_1536_NoMeta --output-dir <预览目录> --suffix before --with-metrics` 导出剥离 EXIF 的预览图（预览模式仅允许更新 `.c1_preview_manifest.json` 登记的自有预览文件，严禁覆盖任何正式交接成片或外部既有 JPG）。
   - 按照脸光源、脸部朝向、受光面与光比从参考库 `索引.md` 选取 1–3 张相近布光样片（新场景先试转 1–2 张交负责转档同事确认后再扩批）。
3. **视觉独立诊断与分层调参（写入前核验受管层签名，劣变自动回滚）**：
   - 每轮视觉审阅须明确记录 **`target_region`（目标区域）、`current_reading`（当前参数与分位读数）、`target_absolute`（传给 `apply` 的目标绝对值，如当前 `+0.20 EV` 需再提亮 `0.15 EV` 则传 `0.35`）、`check_points`（脸颊、白衣高光、窗景/背景复查位置）**，通过 `--note` 随附审计。
   - 调用 `python3 scripts/c1_bridge.py apply --batch-state <batch-state.json> --variant-id <克隆ID> --spec '<JSON>' --note '<本轮依据>'`：
     - **整图色温与色调**：调整 `global.temperature` 与 `global.tint`（局部蒙版白平衡默认禁用；若实验显式开启 `--allow-layer-wb`，快照与回滚同步保存并校验局部色温/色调原值）；全图同向明暗偏差调整 `global.exposure`、`highlight_adjustment`、`shadow_recovery`。
     - **人物与环境分区曝光**：按需启用 `masks.face_skin`、`masks.body_skin`、`masks.clothes`、`masks.subject` 或 `masks.background`（`separate layers` 按身体区域分层而非按人物分层，多人合照须复查全员脸部）。
   - **写入前归属与可恢复性门禁、多参数与多层半途续跑回滚**：拒绝非有限容差（`NaN`/`Inf`）；因飞思 AppleScript 无蒙版光栅读回属性，对新建层与已有受管层均在写前拒绝无法独立核验的 `feather`/`refine` 操作，读回核验绝不跳过 `feather`/`refine`，新建层回滚时直接 `delete` 彻底移除图层；回滚写前持久化 `rollback_in_flight`，支持多滑杆部分恢复中断后的逐字段二分格（`{pre, target}`）续跑与同轮新建多层部分删除后的精确前置删层索引偏移续跑，同时对无关移层、同名替代层或未参与字段异常保持零写阻断。
4. **基于 Inode 身份证明的两阶段导出、原子无损交换（`RENAME_SWAP`）、读取结束连续性核验与冲突锁存解除**：
   - 质检通过的变体运行 `python3 scripts/c1_bridge.py export --mode handoff --batch-state <batch-state.json> --variant-id <克隆ID> --recipe AI_Handoff_Full_sRGB100 --output-dir <正式目录> [--versioned | --overwrite-own]`（共享可写目录优先使用 `--versioned` 生成带调整版本号的新文件名以禁覆盖方式发布；原路径 `--overwrite-own` 强制校验当前用户独占写权限目录）：
     - **Inode 归属证明与旧账本安全兜底**：`pending_export_commit` 记录本次导出创建的精确 `(st_dev, st_ino)`（`candidate_file_ids`）与原收据 `previous_file_id`；升级前留下的缺少可靠 Inode 归属证据的旧待提交记录在加载时一律转入锁存冲突（待人工处理），绝不默认信任内容哈希；
     - **原子无损替换与双路径互换恢复核验**：同卷重导、跨卷重导（先在目标目录完成校验暂存）与备份回滚统一使用内核 `renamex_np(..., RENAME_SWAP)`，崩溃恢复时同时核验正式路径与交换临时路径（`temp_publish_path`），仅在确认被换下文件的 `(st_dev, st_ino)` 与哈希属于原收据后才清理交换临时路径，若互换后中断且换下的是外部文件则完整保留外部文件、锁存冲突并拒绝签发新收据；
     - **覆盖至收据落盘结束的内容连续性核验**：不仅在读取结束时二次复核路径与 Inode，而且在 `save_batch_ledger` 收据写盘结束后再次读取完整文件复核 `SHA-256`、Inode 与大小（即使在收据写盘期间发生等长原地改写也会当场撤回收据并锁存冲突）；
     - **冲突锁存与严格 Inode 校验的显式解除**：一旦登记冲突，重复加载账本绝不自动清除或覆盖现场；显式运行 `resolve-export-conflict --resolution restore-backup` 时，若现场已有文件，必须同时匹配原收据哈希与原收据 Inode（遇到同字节、不同 Inode 的外部文件保持冲突，绝不将其登记为自有）。
   - 逐张运行 `python3 scripts/c1_bridge.py record-handoff --batch-state <batch-state.json> --handoff-file <handoff.jsonl> --record '<JSON>'`：校验变体无未决调整或回滚中断轮次、写入 `verified_adjustment_seq` 与 `verified_sha256` 签名并逐行复核整份 `handoff.jsonl`，仅当 `stale == 0`、`missing_count == 0` 且全量终端状态数等于 `expected_total` 时 `balanced` 才为 `true`。

## 4. 验收与异常恢复

- **完成标准**：源变体与客片原有局部图层零破坏、导出的 JPG 100% 通过五项硬门禁、`handoff.jsonl` 中 `missing_count == 0` 且 `balanced == true`（`expected_total == passed + needs_review + failed`）。
- **中断恢复**：使用原 `--batch-state` 重跑时，`init-batch` 与 `prepare-variant` 自动对齐已有克隆变体，`record-handoff` 精确列出 `missing_photo_ids`，只补做未完成或 `failed` 照片，不重复创建克隆、不覆盖已验收成片。
