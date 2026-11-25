---
name: studio-jianying-bts-editor
description: 按用户指定的已有剪映草稿母版或收藏模板，将照片文件夹逐批替换并本地导出花絮或照片卡点短视频（默认 1080p 30fps MP4），支持验收后安全移入最近删除清理工作副本。
metadata:
  version: "26.11.10"
  status: active
  title: 剪映花絮模板自动制作
---

# 剪映花絮模板自动制作

## 1. 用途与交付

- **适用场景**：用已确认的剪映母版草稿（如 22 槽「萌娃超可爱卡点」，见 [references/validated-template.md](references/validated-template.md)）或收藏模板，将指定文件夹照片逐槽替换并导出短视频；不把从零自由混剪当作默认流程。
- **必要输入**：素材文件夹、目标母版或收藏模板名称、输出 MP4 路径及文件名，以及是否保留工作草稿副本。
- **交付产物**：非空可播放的 MP4 成片（默认 `1080×1920` 竖屏或按模板画幅、`30fps`、`H.264`、`Rec.709 SDR`，关闭独立 MP3 音频导出与云备份）、槽位映射记录、断点状态文件（存于仓库外 `StudioData/outputs/studio-jianying-bts-editor/` 或上游任务目录）及工作副本清理结果。

## 2. 环境、配置与边界

- **支持平台**：当前仅 **macOS 剪映专业版** 经电脑控制会话（UI 自动化 + Node.js ES Modules）验证通过；Windows 尚未适配验证。
- **脚本与界面控制分工**：
  - 分段状态机与界面适配器位于 `scripts/`：`batch_workflow.mjs`、`batch_session.mjs`、`checkpoint.mjs`、`cua_replace.mjs`、`cua_export.mjs`、`cua_cleanup.mjs`、`workflow_metrics.mjs`。
  - 坐标与无障碍元素（AX）必须在**当前会话当前窗口尺寸**下实时确认（注意剪映首页与编辑页窗口尺寸不同会导致旧坐标失效，必读 [references/ui-automation-notes.md](references/ui-automation-notes.md)），严禁写死历史屏幕坐标或直接篡改剪映底层草稿数据库。
- **安全红线**：始终在母版的**独立复制副本**中操作，严禁修改母版、原素材或覆盖已有同名成片；不点击任何付费/会员订阅入口；清理草稿仅限将本次确切工作副本「移至最近删除」，严禁清空回收站或误删母版。

## 3. 执行步骤

1. **选择入口模式**：
   - **固定 22 槽母版批量模式（优先）**：确认母版一致时，按 [references/fixed-workflow.md](references/fixed-workflow.md) 与 [references/efficient-execution.md](references/efficient-execution.md) 执行——首页复制母版 → 进入编辑页核对布局与首槽「复用原视频效果」→ 通过 `advanceWorkflow` 按分段预算（≤52 秒/段）推进 `replace → export → confirm-export → cleanup → complete`。
   - **继续已有草稿 / 新套收藏模板**：续做已有草稿按 [references/existing-draft.md](references/existing-draft.md) 从未完成槽位接续；新套收藏模板依次执行「模板 → 我的 → 收藏 → 加入时间线 → 导入素材 → 展开待替换位 → 替换并逐位核对构图」。
2. **文件选择器与导出面板防坑**（详见 [ui-automation-notes.md](references/ui-automation-notes.md)）：
   - 打开「前往文件夹」（`Cmd+Shift+G`）必须先读界面确认 `PathTextField` 出现后再粘贴本地绝对路径（不传 `smb://` 网络地址），核对无误后只按一次 `Return`。
   - 导出分辨率与帧率优先在母版「草稿参数」预设为 `1080×1920 / 30fps`；中文标题用粘贴（`paste`）防丢字，核对完整输出路径无同名冲突后再提交导出。
3. **导出完成判定与副本清理**：
   - 必须观察到「导出完成」页面且本地目标 MP4 存在非零，才记为导出成功（不凭 100% 进度条或返回编辑页误判）。
   - 导出成功页出现「发布」时，点击它旁边的灰色「取消」（部分版本显示「关闭」），确认已回到编辑窗口。以当前界面按钮文字为准，详见 [弹窗关闭方式](references/ui-automation-notes.md#4-模态弹窗与提示的关闭方式)。
   - 若上游任务要求回存 NAS 并核对通过（或用户未要求保留草稿），关闭编辑页返回首页，核对母版与本次副本标题均精确存在且不同名后，将本次工作副本移入「最近删除」，刷新确认母版仍在且成片完好。

## 4. 验收与异常恢复

- **完成标准**：22（或模板指定）个槽位全部替换无遗漏、目标 MP4 存在且字节数非零、工作草稿按约定移入最近删除或按要求保留。
- **中断恢复**：同一步连续失败两次时，先重新读取完整窗口截图与 AX 排查窗口几何变化或模态弹窗遮挡，仍受阻则保存断点停止，不盲点重试、不重复导出已完成批次。
