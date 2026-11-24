# 局域网 Ollama 看图

本页仅说明现有Ollama专用脚本及其配置，并非技能必须使用的唯一模型。切换到其他线上或本地助手时按[共同模型规范](model-policy.md)执行；若要直接使用其他协议调用脚本，须先完成相应适配与验证，不能只替换端点。

配置由操作人保存在技能外部，不含默认机构：

```json
{"endpoint":"http://SERVER:11434","model":"VISION_MODEL_ID","model_digest":"EXACT_INSTALLED_DIGEST"}
```

endpoint 必须为明确的 HTTP(S) 源地址；不支持带凭证、路径、查询参数。调用绕过环境代理，拒绝重定向；仅向该端点发送无 EXIF 的 JPEG 缩略图、任务重点及精简审美规则。连接地址和机构由可信本地操作人确定，这不是多租户服务鉴权。

先核对素材传输权限及端点授权。通过 /api/tags、/api/show 检查安装与视觉能力，调用 /api/chat，images 为 base64，format 为严格 JSON schema，stream=false、think=false、temperature=0，num_predict=2048。该模型实测仍返回独立 thinking 字段；仅解析 content，思考文本不进入审美报告。输出截断或空内容会停止，不自动重试。协议参考 [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md)。不拉取模型，不自动改服务器监听或防火墙，不回退云端。digest 不一致时停止，避免 latest 被替换后沿用旧验收。

默认最大200张、每张超时120秒、预览长边1536像素；可通过 --max-images（1..1000）、--timeout（1..600）、--preview-size（256..2048）调整。HEIC 等格式若本机无解码器会停止，不跳过后凑数。失败保留 model-review.json，不能直接用部分完成结果复制；--resume 核对上下文、哈希及输出，不重新发送完成图片。异常中断留下 .tmp 或不完整导出时，人工核对后移到独立恢复目录，禁止盲目删除或重放复制。

模型返回 quality、aesthetic（0..100）、subject_match（0..1）、people_count、eligible、reason。它们是主观预览判断；不能替代原图锐度检测、人物身份核对或整套覆盖度审阅。无重点：score=0.4×quality+0.6×aesthetic；有重点：score=0.7×subject_match×100+0.3×前述分数。这是透明的初始排序策略，尚未通过员工选片准确率验证。eligible 仅过滤用户明确的硬条件；不能因普通审美分歧拒绝图片。

“弟弟/姐姐”若无可见外观描述不能可靠识别；请先由人工确认人物，并复核 model.reason。逐张判断没有跨图人物记忆。group 默认为空，近似重复仍由整组联系表复核；精确文件重复由 SelectionTool 哈希校验。模型不决定路径、文件名或复制动作。

米乐 profile 是独立 candidate 规则文本，未训练进模型。只传规则与限制，不发送客户文件名、示例照片、员工姓名或证据路径。切换机构时显式更换 organization/profile。报告、预览及配置按原素材的访问范围和保存期限管理。

验收：28项隔离测试覆盖协议、范围校验、跨机构拒绝、断点、输入变更和只读行为；3090的 VISION_MODEL_ID 实测合成图接口。真实客户照片审美、真实多机运行、独立审核尚未完成，保持 draft / production-ready=false。
