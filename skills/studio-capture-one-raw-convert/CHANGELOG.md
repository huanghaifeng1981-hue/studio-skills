# studio-capture-one-raw-convert 更新记录

## 26.11.28（2026-11-27 · 真实客片全链路验收通过，正式转为 `active`）

- 完成 macOS + Capture One Pro 16.9 真实客片多场景端到端验收（覆盖新建会话、源变体隔离克隆、`--retouch-style Studio_Default_Retouch` 自动人像润饰+锐化、NAS 正式参考库对照、AI 面部皮肤蒙版分层调参、多轮微调与 `--overwrite-own` 无损原位覆盖重导、原始完整尺寸 sRGB 100 质量 JPG 五项门禁与 `handoff.jsonl` 账本对平），经用户明确确认效果达标，状态由 `draft` 正式升级为 `active`。

## 26.11.27（2026-11-27 · Capture One Pro 16.9 实机客片试跑验收与 5 项真实接口适配修复）

- **自定义导出目录设置顺序修复**：`build_export_script` 改为先设置 `root folder location of rRef to (POSIX file stagingDirPath as alias)` 再设置 `root folder type of rRef to custom location`，修复 Capture One 16.9 静默回退至会话默认 `Output` 目录的问题。
- **导出临时文件兼容**：`wait_for_staged_jpg` 允许同 stem 的 `.jpg.tmp` / `.jpeg.tmp` 写入中临时文件存在，待写入完成转正为正式 `.jpg` 后再校验，同时继续拦截任何无关文件。
- **AI 人物蒙版新建层定位修复**：`build_apply_script` 创建 `create people mask ... with separate layers` 后改为通过新建前后图层计数定位 `last layer of vRef`，避开 Capture One 16.9 返回嵌套列表导致的重命名报错（`-10006`）。
- **色轮 0°/360° 同角容差**：`within_tolerance` 对 `*_hue` 角度字段按模 `360°` 计算最短圆周差，修复飞思写 `0.0` 读回 `360.0` 导致回滚核验误报。
- **正式导出尺寸门禁兼容**：`validate_handoff_export` 兼容 Capture One 16.9 已含竖图旋转的 `raw_dimensions` 以及去马赛克/镜头校正产生的 `±2 px` 偶数像素对齐差异。
- **参考库默认口径**：`SKILL.md` 优先指向本机工作站配置 `StudioData/config/studio-capture-one-workstation.md` 中登记的 NAS 正式参考库。

## 26.11.26（2026-11-27 · 十一审最终收口：消除进程内与冲突解除对交换临时文件的盲删、补齐两段可恢复收据提交与前滚后置内容复核、消除换回后共享 Inode）

- **关闭十一审 P1 #1（进程内异常处理盲删 `temp_publish_path` 导致被 `RENAME_SWAP` 换下的外部文件丢失）**：移除 `handle_in_process_abort_unlocked` 与 Phase 3 异常分支对 `temp_publish_path` 的无条件 `unlink`，所有临时文件清理统一经 `inspect_swap_temp_path_for_recovery_unlocked` 归属核验后放行；`RENAME_SWAP` 后若抛普通异常（如 `OSError`），被换下外部文件完整换回正式路径并锁存冲突。
- **关闭十一审 P1 #2（`resolve-export-conflict` 无条件删除 `temp_publish_path` 销毁尚未换回的外部文件唯一副本）**：`resolve_export_conflict` 清理 `temp_pub_path` 前强制经 `inspect_swap_temp_path_for_recovery_unlocked` 核验；若其中存有与正式路径不同 Inode 的未归属外部文件，绝不删除，而是原子转存至 `.c1_conflict_rescued_*` 并在返回结果中明确报告 `preserved_temp_external_path`。
- **关闭十一审 P1 #3（收据落盘后复核存在崩溃撤回窗口、撤回写盘失败静默吞错、重启前滚缺少落盘后内容复核）**：`commit_handoff_export_atomically` 升级为两段可恢复提交（Stage 3a 持久化 `receipt_pending_post_verify` 并保留 `pending_export_commit`、经后置全文 `SHA-256` 校验后才于 Stage 3b 最终签发并再次复核），`persist_revoked_receipt_and_conflict` 写盘失败明确抛出严重错误；`load_batch_ledger` 重启前滚保存收据后同样逐张执行 `inspect_destination_state_unlocked` 全文 `SHA-256 + inode + size` 连续性复核，检出等长原地改写立即撤回收据并锁存冲突。
- **关闭十一审 P2（中断换回恢复后把正式文件硬链接回隐藏临时路径）**：`rollback_aborted_commit_or_detect_conflict_unlocked` 将被换下外部文件原子换回 `target_path` 后，仅核验并删除己方候选临时文件，不再执行 `os.link(target_path, temp_pub_path)`，保持客户正式文件 `st_nlink == 1`。

## 26.11.25（2026-11-27 · 吸收 GPT 十审关闭原子互换中断双路径核验、收据落盘后完整内容连续性复验、restore-backup 同字节外部 Inode 拦截与旧待提交记录冲突锁存）

- **关闭十审 P1 #1（`RENAME_SWAP` 原子互换成功后中断，重启恢复删除 `temp_publish_path` 中被换下的外部文件并误签新收据）**：恢复时通过 `inspect_swap_temp_path_for_recovery_unlocked` 同时核验正式路径与交换临时路径 `temp_publish_path`，仅当确认被换下文件的 `(st_dev, st_ino)` 与 `SHA-256` 属于原收据（或属于本次未互换候选临时文件）后才允许清理与前滚；若换下的是外部文件，完整保留外部文件、撤回 `export_verified` 并锁存冲突，连续多次重载均不丢失外部文件、不签发新收据。
- **关闭十审 P1 #2（收据写入期间发生等长原地改写仍返回成功）**：在 `save_batch_ledger` 收据写盘结束后再次调用双遍读取的 `inspect_destination_state_unlocked` 完整复核现场文件的 `SHA-256`、`(st_dev, st_ino)` 与字节大小；无论是首次发布还是覆盖重导，一旦在收据写盘期间发生等长原地改写，当场撤回收据（`export_verified=false`、`export_receipt=null`）、锁存冲突并报错退出。
- **关闭十审 P1 #3（`restore-backup` 将同字节、不同 Inode 的外部文件登记为自有，导致后续 `--overwrite-own` 覆盖外部文件）**：`resolve_export_conflict` 在现场已有文件时同时强制核验原收据的 `previous_sha256` 与 `previous_file_id` (`(st_dev, st_ino)`)；遇到同字节、不同 Inode 的外部文件时拒绝解除并保留冲突，后续 `--overwrite-own` 继续阻断，外部文件完好保留。
- **关闭十审 P1 #4（升级前留下的不含 `candidate_file_ids` 的旧 `pending_export_commit` 在加载时默认信任同内容外部文件并误签收据）**：新增 `has_reliable_pending_ownership_proof`，彻底移除 `else True` 回退；旧待提交记录缺少可靠 Inode 归属证据时一律转入 `legacy_unverified_pending_commit` 锁存冲突状态，首次及后续再次加载均保留外部文件且绝不授予所有权或成功收据。

## 26.11.24（2026-11-26 · 吸收 GPT 九审关闭同内容先到文件 Inode 保护、原子 RENAME_SWAP 无损替换、读取期间换档拦截、锁存冲突显式解除与预览核验失败阻断）

- **关闭九审 P1 #1（首次发布失败或中断恢复误删/误认同字节先到文件）**：`pending_export_commit` 记录本次导出创建的精确 `(st_dev, st_ino)`（`candidate_file_ids`），将“字节内容相同”与“本次导出实际创建发布的 Inode”彻底分离；先到的同内容外部文件在发布报错与崩溃恢复时均原封不动保留且绝不签发所有权收据。
- **关闭九审 P1 #2（同卷、跨卷覆盖重导及回滚窗口覆盖刚换入的第三份文件）**：新增共享写目录拦截 `verify_exclusive_output_dir` 与 `--versioned` 新版本文件名禁覆盖发布；原路径覆盖（同卷与跨卷均先在目标目录预暂存）及备份回滚统一改用 macOS 内核 `renamex_np(..., RENAME_SWAP)` 原子无损互换并复核被换下 Inode，一旦发现窗口内闯入第三份文件立即原子换回原位并报告冲突，严禁回退 `os.replace`。
- **关闭九审 P1 #3（读取大图期间正式路径被换档仍签发成功收据）**：`inspect_destination_state_unlocked` 在哈希读取结束时再次 `lstat` 复核路径仍指向被读取的同一 `(st_dev, st_ino)` 且大小/时间戳未变，并在账本落盘后二次锚定 Inode，读取期间换档在首次导出与覆盖重导下均阻断成功收据。
- **关闭九审 P2 #4（已持久化的冲突在重启加载账本时被自动清除或恢复覆盖）**：`reconcile_pending_export_commits_unlocked` 对已登记 `conflict` 的记录严格锁存（连续多次重载均保持正式路径、备份与冲突记录不变），新增 `resolve-export-conflict`（`keep-external` / `restore-backup`）作为唯一显式解除入口。
- **关闭九审 P2 #5（预览现场核验失败仍返回成功结果）**：`--mode preview` 发布后若现场类型、Inode、哈希或大小不符（被替换或被移除），保留现场并立即抛出 `BridgeError` 返回失败。

## 26.11.23（2026-11-26 · 吸收 GPT 八审关闭进程内与重启统一冲突保护、纯内核原子禁覆盖发布、收据落盘前现场复核与悬空符号链接拦截）

- **关闭八审 P1 #1（覆盖重导进程内报错后异常处理用旧备份覆盖外部第三份文件）**：抽取统一的 `rollback_aborted_commit_or_detect_conflict_unlocked` 状态机供进程内异常（Phase 2 发布失败 / Phase 3 提交失败）与进程重启恢复（`reconcile_pending_export_commits_unlocked`）共用，无论进程继续运行还是重启，遇到第三份外部文件均逐字节保留现场、旧备份与 `pending_export_commit` 待恢复记录并报告冲突。
- **关闭八审 P1 #2（硬链接与 `RENAME_EXCL` 均不可用时回退 `os.replace` 可能覆盖先到同名文件）**：`publish_verified_temp_no_overwrite` 彻底移除 `exists() + os.replace()` 非原子回退分支，目标文件系统缺少内核级原子禁覆盖能力时立即删除临时文件并报错退出，确保先到文件原字节保留且绝不签发成功收据。
- **关闭八审 P1 #3（发布后、收据落盘前正式文件被替换仍签发成功收据）**：Phase 3 在提交收据前通过 `inspect_destination_state_unlocked`（`lstat` + `O_NOFOLLOW` + `fstat`）复核正式路径必须为普通文件且大小与 `SHA-256` 匹配本次新文件；若已发生替换，逐字节保留现场、备份与待恢复记录，拒绝签发收据并抛出 `EXPORT RECOVERY CONFLICT`。
- **关闭八审 P2 #4（悬空符号链接跟随 `.resolve()` 把 JPG 写到输出目录之外）**：新增 `canonicalize_leaf_path` 仅规范化父输出目录、保留最终文件名不做 `.resolve()`，导出与交接入口均对叶子路径做 `lstat` 符号链接拦截，悬空符号链接保留原样、外部目标不创建、账本不登记成功。

## 26.11.22（2026-11-26 · 吸收 GPT 七审关闭崩溃恢复外部文件保护、预览清单提交恢复、无硬链接零半文件发布、多层部分删除续跑与新建层蒙版写前拦截）

- **关闭七审 P1 #1（崩溃恢复覆盖外部替换的第三份 JPG）**：`reconcile_pending_export_commits_unlocked` 在正式路径哈希既不等于 `previous_sha256` 也不等于 `new_sha256` 时，逐字节保留现场第三份文件、保留 `backup_path` 备份与 `pending_export_commit` 待恢复记录，并立即抛出 `EXPORT RECOVERY CONFLICT` 报告冲突。
- **关闭七审 P1 #2（正式 JPG 替换后预览清单写盘失败丢失恢复依据）**：将 `unregister_from_preview_manifest` 与收据落盘统一纳入替换后提交恢复阶段（`save_preview_manifest` 抛错当场从备份恢复旧片或撤回首次导出新片，且在账本落盘成功前绝不提前删除备份；重启前滚恢复同步完成预览清单清理）。
- **关闭七审 P1 #3（不支持硬链接时首次导出中断残留半份正式 JPG）**：移除直接在 `final_path` 上 `O_EXCL` 流式写入的逻辑，改为在目标目录隐藏临时路径完成复制、`fsync` 与完整 SHA-256 校验后，再通过禁覆盖原子发布（`renamex_np(..., RENAME_EXCL)` / 锁内非覆盖切换）落盘，真实子进程 `SIGKILL` 中断后正式路径零半文件残留、恢复后原路径可直接继续导出。
- **关闭七审 P2 #4（同轮新建多层部分删除后回滚无法续跑）**：`verify_managed_layers_before_rollback` 根据已确认删除的前置本轮新建层（`orig_deleted_idx < layer_orig_idx`）精确计算合法栈索引下移量，支持多层删层中途续跑完成，同时继续零写阻断无关移层与同名替代层。
- **关闭七审 P2 #5（新建层 `feather` / `refine` 无法通过正式序列化器独立核验）**：因飞思 AppleScript 序列化器无 `feather`/`refine` 读回属性，`reject_unverifiable_mask_ops` 在写前对新建层与已有层统一零写拒绝这两项操作，且全部单元测试读回数据严格与正式序列化器字段对齐。

## 26.11.21（2026-11-26 · 吸收 GPT 六审关闭两阶段导出崩溃恢复、多参数半途回滚续跑、迟到旧导出无损拒绝与蒙版羽化门禁）

- **关闭六审 P1 #1（正式 JPG 替换后收据写盘失败/强退导致丢失上一版成片）**：`commit_handoff_export_atomically` 升级为崩溃一致的两阶段提交——替换前创建硬链接备份并持久化 `pending_export_commit` 意图日志；若替换后收据保存抛错，当场从备份原子恢复上一版成片；若提交前后遭遇进程强退，重启加载 `load_batch_ledger` 时经 `reconcile_pending_export_commits_unlocked` 自动回滚或前滚至文件与收据完全一致状态，确保后续 `--overwrite-own` 持续可用。
- **关闭六审 P1 #2（同一图层只恢复部分参数时回滚无法续跑）**：`verify_layer_matches_signature` 在续跑回滚时改为逐字段二分格（`{pre_rollback, target_rollback}`）核验，允许部分参数已恢复的合法中间态（如 `(1, 20) -> (0, 20)`）顺利续跑完成至 `(0, 0)`，同时对未参与回滚字段漂移或同名替代层继续执行零写阻断。
- **关闭六审 P2 #3（迟到的旧版本导出撤销新版有效收据的 `export_verified`）**：过期导出（`started_adjustment_seq != current_seq`）被拒绝时不再修改账本中已存在的新版本有效收据与 `export_verified` 标志。
- **关闭六审 P2 #4（已有受管层 `feather`/`refine` 缺少读回核验与可验证回滚）**：`verify_readback` 移除对 `feather`/`refine` 的跳过豁免（读回无结果立即判失败）；`verify_recoverable_originals_before_write` 对已有受管层在写前零写阻断不可快照恢复的 `feather`/`refine` 操作；新建受管层回滚时改为 `delete targetLayer` 彻底删除图层并核验移除。

## 26.11.20（2026-11-26 · 吸收 GPT 五审关闭回滚中断持久化、无签名建层归属保护、单锁原子重导提交、旧账本迁移与 NaN 拦截）

- 回滚发写命令前持久化 `rollback_in_flight`、阻断无建后签名同名层禁用、单锁校验重导、迁移旧收据并全面拦截 `NaN`/`Inf`。

## 26.11.19（2026-11-26 · 吸收 GPT 四审关闭失败轮次交接阻断、全状态回滚写前校验、原路径重导归属分离与空原值写前拦截）

- 阻断未决失败轮次的正式导出与 `passed` 交接、补齐失败态回滚写前校验、分离 `owned_export_files` 与 `export_receipt`、拦截空原值覆写。

## 26.11.18（2026-11-26 · 吸收 GPT 三审关闭同干名跨扩展名、交接实时失效、预览防覆盖与受管层指纹核验）

- 支持跨扩展名同名选片独立入账、交接记录跨轮实时置 `stale`、预览禁覆写正式成片及受管层签名校验。

## 26.11.17（2026-11-26 · 吸收 GPT 二审关闭选片核对、导出收据、原子链接、同名层隔离与预写 WAL）

- 引入 `adjustment_seq` 与 `SHA-256` 收据、内核级 `os.link` 原子落盘、客片继承层快照隔离及 WAL 预写式回滚。

## 26.11.16（2026-11-26 · 吸收 GPT 一审修复读回容差、独立暂存导出、五项门禁与批次账本）

- 补齐 28 项属性序列化与按单位容差读回校验（`exposure: 0.02 EV`），建立独立暂存导出与五项硬门禁，引入 `batch-state.json` 源变体防火墙。

## 26.11.15（2026-11-26 · 新增飞思 AppleScript 确定性控制桥接脚本与分区调光闭环）

- 新增确定性控制脚本 `scripts/c1_bridge.py` 与初始单元测试，打通 Capture One `16.9.0.10` 字典静态编译校验。

## 26.11.14（2026-11-24 · 清洗迁入 studio-skills 新仓并与 StudioData 解耦）

- 迁入 `studio-skills` V2 仓库，将本地参考样片库与工作站配置说明统一归位至仓库外 `StudioData/references/米乐转档参考库/` 与 `StudioData/config/`。

## 26.11.13 及以前（旧仓历史摘要）

- 确立 macOS Capture One AppleScript 原生接口操作、以人物脸部曝光为基准的四分区蒙版校正、逐张视觉独立决策（禁套基础参数）、全量默认润饰及不裁切不缩放 100 质量 sRGB JPG 交接规范。
