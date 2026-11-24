# studio-feishu-wiki-sync 更新记录

## 26.11.5（2026-11-24 · 迁入 studio-skills 新仓）

- 迁入 `studio-skills` V2 仓库并按四段式标准重写 `SKILL.md`，状态更新为 `active`（macOS 真实知识库双向验证通过）。
- 显式关联包内 `references/` 下的 `lark-cli` 读写、转义与身份契约参考文档，强调局部块更新以保护飞书页内评论。
- 移除过时的“仅根目录直属文档用固定名”假设，改为遵循接收项目命名规范，并补齐上传/下载时的「本地 Markdown 相对链接 ↔ `feishu_url`」双向转换与必做回读验收要求。

## 26.11.4 及以前（旧仓历史摘要）

- 收录飞书知识库双向同步规则，移出门店硬编码文档映射，统一通过调用方本机 `lark-cli --as user` 操作并回填 `feishu_url`。
