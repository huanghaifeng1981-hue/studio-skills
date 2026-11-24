# studio-skills

干净、轻量的自建 AI 技能仓库（V2）。采用**代码与数据彻底分家**架构：本仓库只保存纯技能逻辑与测试，所有模型、配置和运行数据统一放在仓库外的 `StudioData/`。

## 技能目录

| Skill (英文 ID) | 中文名称 | 版本 | 状态 | 用途说明 |
|---|---|---|---|---|
| [studio-feishu-wiki-sync](skills/studio-feishu-wiki-sync/SKILL.md) | 飞书知识库同步 | 26.11.5 | active | 按口令双向同步本地 Markdown 与飞书知识库（含链接互转与回读核验） |
| [studio-photo-folder-match](skills/studio-photo-folder-match/SKILL.md) | 照片文件夹查找 | 26.11.3 | active | 根据选片表/摄影表匹配客人底片目录，支持精确姓名断点安全复制 |

## 目录结构（代码仓 vs 数据目录）

```text
~/Documents/ChatGPT/
├── studio-skills/          # 【本仓库 · 传 GitHub】纯代码与文档，体积 < 1 MB
│   ├── AGENTS.md           # 智能助手唯一规范入口
│   ├── README.md           # 技能清单与操作指南
│   ├── CHANGELOG.md        # 仓库级里程碑记录
│   ├── scripts/install.py  # 技能安装与备份脚本
│   ├── templates/          # 新技能模板与外部配置示例
│   ├── tests/              # 仓库级自动测试
│   └── skills/             # 各独立技能包（各自含 SKILL.md、scripts/、tests/、CHANGELOG.md）
│
└── StudioData/             # 【本机数据 · 不进 Git / 不进 iCloud】大文件与私有配置
    ├── config/             # 各技能本机配置（账号、群名、路径、环境变量名）
    ├── models/             # 本地大模型与专用推理环境
    ├── references/         # 门店词表、转档参考样片库等静态参考
    └── outputs/            # 运行产物、日志与试跑临时文件
```

## 安装与更新

默认预览变更，加 `--apply` 才实际安装，原有同名技能会自动备份到目标同级的 `skill-backups/`，并保留已有符号链接：

```sh
# 预览全部技能安装计划
python3 scripts/install.py

# 实际安装全部技能（默认安装到 ~/.codex/skills）
python3 scripts/install.py --apply

# 只安装某一个刚审核完的技能，或指定目标目录
python3 scripts/install.py --skill studio-feishu-wiki-sync --apply
python3 scripts/install.py --target ~/.gemini/config/skills --apply
```

其他电脑只读获取更新（不在运行机直接改技能源码，彻底杜绝冲突）：

```sh
git pull --ff-only
python3 scripts/install.py --apply
```

## 从旧仓迁入技能的四步标准动作

每次挑 1 个技能，对助手说“把旧仓的 `<技能名>` 审核整理后迁入 `studio-skills`”，按以下四步闭环：

1. **规范命名与剥离数据**：所有自建技能英文 ID 统一以 `studio-` 开头（旧仓未带前缀的技能迁入时统一补齐，并同步更新关联调用名）；把包内残留的本机路径、配置移到 `../StudioData/config/`，代码里统一通过参数或环境变量读取。
2. **精简文档结构**：对照 `templates/skill/SKILL.md` 理顺说明，标清支持平台与真实已验证边界，版本从新起点或延续原版标清，建立包内 `CHANGELOG.md`。
3. **跑通验证**：补齐或运行该技能测试及仓库 `python3 -m unittest discover -s tests -p 'test_*.py'`。
4. **登记入库**：更新本页「技能目录」表格与仓库 `CHANGELOG.md`，检查 `git status` 无任何敏感文件后提交。
