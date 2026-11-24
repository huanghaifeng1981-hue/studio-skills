# 本地 MOSS 安装与配置

安装/迁移本技能、缺依赖或模型目录时读取。技能安装仅复制方法和脚本，不自动安装Python依赖或模型。以下是可选MOSS入口，其他具备音频能力的工具仍可按同一业务规则执行。

## 支持范围

已验证macOS ARM64 / Python3.12 / CPU float32：一分钟样本及约6分钟录音可生成文字、时间和匿名说话人。Windows为目标平台，以下命令尚未经Windows实测。长录音速度、内存和识别质量须目标机器验收。不把CPU入口描述为GPU加速。

## 安装

先选技能包外运行目录，在该目录建立虚拟环境；已有同名环境先检查并备份，不覆盖。

macOS：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r "技能目录/references/requirements-moss.txt"
.venv/bin/python -m pip check
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r "技能目录\references\requirements-moss.txt"
.\.venv\Scripts\python.exe -m pip check
```

依赖文件固定关键版本，不是完整跨平台锁文件；pip找不到对应平台版本时停止并报告，不静默改用云端。模型从官方 `OpenMOSS-Team/MOSS-Transcribe-Diarize` 下载，固定所选完整revision并记录在包外部署记录。可通过huggingface_hub的snapshot_download(repo_id, revision, local_dir)下载；这一步仅下载，不传入音频。官方模型含自定义Python实现，执行前检查来源与代码，离线加载时会执行这些文件。

## 包外部署记录

运行目录保留deployment.json，字段由调用方填写：python_executable、model_directory、model_repository、model_revision、vocabulary_file、output_directory、device（本入口仅cpu）、skill_version、platform及验证范围。路径按目标电脑重建，不能照搬他人路径。该记录供助手读取并映射到CLI参数，脚本目前不直接解析deployment.json。

词表可按vocabulary.example.json建立，存到包外；门店姓名、偏好、密钥和真实路径不入包。模型与虚拟环境也不放在技能目录。

## 处理与检查

1. 确认模型文件完整、本地目录可读；运行transcribe_moss.py --help，并执行pip check。
2. 可直接输入m4a/WAV，包内audio_io.py通过PyAV在本机转为16kHz单声道数组，原音频不变；处理后核对时长，不能仅修改扩展名。
3. 设置HF_HUB_OFFLINE=1和HF_HUB_DISABLE_TELEMETRY=1；HF_MODULES_CACHE设到包外可写目录。分别用shell的export或PowerShell的$env:设置。
4. 用虚拟环境Python调用包内脚本，传入音频、--model、--output；有词表时传--vocabulary；按需传--scene customer/meeting/training。选择新输出目录，避免覆盖人工修订。
5. 先测一分钟真实录音，检查金额、姓名、漏段、换人位置及速度，再处理全文；查看metadata.json的生成上限和待审阅状态。单独确认格式偏好已应用到展示稿，原始raw.txt不改；脚本不自动完成全部人工校正和展示格式整理。

## 迁移与恢复

随技能携带：本说明、依赖清单、无真实值的词表示例和脚本。单独迁移：deployment.json、门店词表/格式偏好，以及用户选择保留的录音和结果。模型可复制完整快照或按固定revision重下载；Python虚拟环境在目标系统重建，保留原机环境及配置。完成迁移时主动向用户逐项说明哪些已迁移、哪些尚未完成，不因技能文件复制成功就称环境已就绪。
