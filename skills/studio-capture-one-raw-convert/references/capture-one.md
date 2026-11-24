# macOS Capture One 操作

## AppleScript 接口与批次

- 用 macOS「脚本编辑器」(Script Editor) 打开 Capture One 的脚本字典。也可从飞思 Scripts → Open Scripting Dictionary 打开字典。正式调用由 `osascript` 直接发给 Capture One。Automator（自动操作）可以启动 AppleScript，但技能运行不依赖 Automator。
- 在目标机确认 Capture One 版本和 macOS 自动化权限。使用专用集合，核对引用的是原位置 RAW。先按文档、源文件路径和变体 ID 定位。已有编辑时使用字典的 `clone variant`，后续显式指定克隆后的变体。
- 本机参考字典 16.9.0.10 提供 `adjustments` 下的 `exposure`、`temperature`、`tint`、`highlight adjustment` 和 `shadow recovery`。写入绝对值并读回。基础调整由视觉模型逐张看图决定，不复制参考数值，也不以自动调整替代逐张判断。
- 导入及导出依赖已验证的设置和配方。接口权限、变体定位或 JPG 导出失败时保留断点并报告失败原因。

## 基础调节项

下表是本机 Capture One 16.9.0.10 字典的属性。目标机首次启用某项前，在样片上验证读取、写入、读回和渲染。保存实际改动值供本张重试与复现；同场景其他照片也须独立看图，不套用这些值。

| 飞思工具 | AppleScript 属性 | 使用判断 |
| --- | --- | --- |
| 白平衡 | `temperature`、`tint` | 修正光源偏色，保留有意冷暖。 |
| 曝光 | `exposure` | 校正主体整体亮度。 |
| 高动态范围 | `highlight adjustment`、`shadow recovery`、`white recovery`、`black recovery` | 按需恢复高光与阴影层次，检查白衣、额头与深色服装。 |
| 色阶 | `level shadow rgb`、`level midtone rgb`、`level highlight rgb`，以及各通道和输出端属性 | 按需调整黑白点或中间调，检查剪切。通道色阶只在有明确偏色时采用。 |
| 曲线 | `luma curve`、`rgb curve`，以及各色通道曲线 | 针对特定亮度范围适度修正。按目标机字典验证曲线点写入与读回。 |
| 色彩平衡 | `color balance master …`、`color balance shadow …`、`color balance midtone …`、`color balance highlight …` | 按需消除局部影调的残余色偏。 |
| 清晰度 | `clarity method`、`clarity amount`、`clarity structure` | 在局部反差确有需要时轻调，检查皮肤纹理和轮廓。 |

## 曝光蒙版

| 区域 | 用途 | 本机 16.9.0.10 脚本字典 |
| --- | --- | --- |
| 面部皮肤、身体皮肤 | 以同布光参考的脸颊和额头为准。身体有独立问题时再调整，保留脸、颈、手自然衔接。 | `create people mask` 的 `face skin`、`body skin` 区域，可分层。 |
| 主体 | 衣服、人物整体需要与背景分开调整时使用。注意与皮肤蒙版重叠。 | 图层类型 `subject mask`。 |
| 背景 | 背景过亮、过暗或需要局部恢复层次时使用。检查人物边缘和发丝。 | 图层类型 `background mask`。 |

写入时使用目标图层的 `adjustments`。在目标机用样片验证创建、覆盖范围、曝光或高动态范围参数读回及渲染。脸是曝光判断基准，整张同向偏差先调整体，脸单独偏差用皮肤蒙版。按需调整身体皮肤、主体和背景后，最后回查脸部曝光、肤色、脸颈衔接与蒙版重叠。只有仍有全局明暗偏差时才再次微调整体。局部曝光与皮肤润饰分别记录。

### 蒙版验证与曝光示例

上表只说明 16.9.0.10 字典存在入口，不代表四类蒙版已通过实拍。每类分别记录「字典已核对、创建已跑通、参数读回通过、渲染验收通过」及版本、样片和结果，未完成的保留待验证状态。已有 8 张润饰验收不代替曝光蒙版验收。

- 人物区域显式传入 `areas`；需分别调整面部与身体时设 `separate layers` 为 true（省略时默认合并）。使用命令返回的图层列表定位，核对已有图层后再重试，避免重复创建。
- 主体、背景的创建语句须样片跑通；`kind` 是只读属性，不假定能创建后直接改写。`background mask` 是背景蒙版，`background` 是基础 Image 层，不能混用。
- 在测试克隆上给目标层临时写入明显曝光差，读回并导出前后预览，检查实际覆盖；验证后恢复正式值并重新渲染。读回成功不能证明蒙版覆盖正确。检查脸、颈、手、衣服、发缘和多人漏选，全程使用原生脚本。

| 场景 | 调整顺序与验收 |
| --- | --- |
| 白衣白墙 | 脸、衣服、背景一起偏暗或偏亮，先整体曝光接近目标；只有脸偏差则直接面部皮肤。再按需调身体、主体和背景，最后回查脸、额头及毛衣纹理，不按整图偏白压暗脸。 |
| 金色逆光 | 按实际照脸光匹配参考，保留暖轮廓光；只有脸暗用面部皮肤，全图同向偏差才先整体。分区后回查脸部肤色、肩部白衣高光和发缘。 |
| 脸上斑驳光 | 分别看亮斑与暗区，不能用整张面部统一提亮来补暗区而损失亮部。按备注保留有意光斑；现有已验证接口不能可靠兼顾时，保留最佳变体并标待复核。 |

## 皮肤润饰

米乐默认值来自用户提供的 3136、3163 润色样片与截图，用户确认了 8 张脚本润色对照效果。表中为飞思界面显示值。使用验收变体复制时保留飞思内部精度。

| 面板 | 默认值 |
| --- | --- |
| 面部皮肤 | 冲击 60，去瑕疵 90（含颈部），黑眼圈 50，匀肤 65、质地 0（含颈部），轮廓 50 |
| 碎发 | 外层 80，内层 80 |
| 牙齿 | 亮度 46，去饱和度 42 |
| 双眼 | 虹膜 40，巩膜 40 |
| 身体皮肤 | 匀肤 70，质地 0 |
| 肤色 | 全部皮肤均匀度 20，色温、色调、饱和度、亮度均为 0 |

闭眼、无露齿或皮肤被遮挡时，对应项目按可见区域检查。牙齿设置来自截图，首批样片没有露齿，视觉效果待后续验证。

- Retouch Face Skin 自飞思 16.6 提供。全部选中照片使用上表已确认的默认值，应用后检查观感。检查毛孔与细节，保留痣、雀斑等人物特征。异常记入待复核，不臆造字典未提供的保护或单项调节入口。
- Skin Tone Retouching 自 16.9 提供。使用上表肤色默认值处理同一人面部与身体的色差，核对自动皮肤蒙版和每人的目标肤色。多人合照逐人核对，保留不同人物本身的肤色。是否能自动应用以本节的脚本验证结果为准。
- 16.9.0.10 的 AppleScript 字典未提供直接写这些润饰滑杆的属性。已验证在同一测试会话中对用户润色后的 JPG 源变体执行 `copy adjustments`，再对 RAW 变体执行 `apply adjustments`，可随目标图重新识别人物并生效。用半成品 JPG 加其飞思设置作为源时，选择带润饰设置的源变体，核对其与本页默认参数一致。只有渲染结果通过才算成功。
- 复制仅用于带入默认润饰，不用于套用源片曝光等基础参数。首次批量应用前，在克隆变体对照复制前后的曝光、白平衡、高光阴影、曲线、色阶、几何设置及已有局部图层；检查读回值和渲染是否变化。发现夹带调整时，恢复目标设置并核验；无法可靠恢复或润饰应用失败时标为待复核，不能当作已完成默认润饰交付。保留基础转档变体用于对照和恢复。复制与应用使用同一文档的设置剪贴板，按文档串行执行并明确源、目标变体。仅有导出的 JPG 时，需提供带已保存设置的源变体。
- 与保留的基础转档变体渲染作对照，不依赖尚未验证的关闭润饰接口。记录默认参数版本、源文档与变体、实际工具及应用结果；不因皮肤本身好而跳过润饰。闭眼、未露齿等仅说明对应效果不可见，不改变默认参数。转档通过与否以实际交接 JPG 为准。转档质检通过但仍有皮肤问题时，将该问题一并交给精修诊断。

## 预览与导出

- 将预览配方设为低分辨率 sRGB JPG，单独存到临时目录，供同一变体前后对比。给视觉模型的预览留足主体细节，外发前确认配方关闭 EXIF、GPS 等元数据。交接只启用一个 JPG 配方，固定质量 100、sRGB，不裁剪、不缩放，保留 RAW 原始完整画面和像素尺寸，输出到独立目录。批量导出前核对启用的配方、质量读回值及预期原始像素尺寸。若已有裁切，在转档克隆变体中通过已验证的原生接口恢复完整画面，保留原有变体；不能恢复时标为待复核。正常横竖方向显示需正确换算宽高，不把 RAW 存储方向或传感器非成像边缘当作交付尺寸。
- AppleScript 的 `process … recipe …` 可指定变体与配方。处理前确认配方位置、命名模板及冲突处理。返回值可能是作业 ID 或带 `ERROR` 前缀的错误，仍需等实际文件写完。版本或权限不支持时报告该批次的导出阻塞。未确认队列状态前先查输出与活动，避免重复导出。
- 预览量化命令。使用 Python 3.9+ 和 macOS 自带的 sips。将输入替换为实际的、同配方导出的 JPG，可一次传多个文件。

```sh
python3 scripts/preview-metrics.py "/path/to/before.jpg" "/path/to/after.jpg"
```

每张输出一行 JSON。亮度分位采用 0–255 的 sRGB 编码亮度。近黑与近白比例分别统计亮度 ≤ 5 和 ≥ 250 的采样像素。该指标来自渲染图的缩略采样，可比较渲染变化，RAW 通道剪切需在飞思另行检查。

## 官方文档

- [Auto Adjustments](https://support.captureone.com/hc/en-us/articles/360002609238-Auto-adjustments)
- [Smart Adjustments 与曝光](https://support.captureone.com/hc/en-us/articles/360002785697-Exposure)
- [AppleScript 自动化](https://support.captureone.com/hc/en-us/articles/360002681418-Capture-One-Workflow-Automation-with-AppleScript)
- [Export Recipes](https://support.captureone.com/hc/en-us/articles/360021057158-Export-Recipes)
- [Retouch Face Skin](https://support.captureone.com/hc/en-us/articles/27336176639133-Retouch-Face-Skin-tool)
- [Skin Tone Retouching](https://support.captureone.com/hc/en-us/articles/39849323415453-Skin-Tone-Retouching)
- [Capture One 16.9 版本说明](https://support.captureone.com/hc/en-us/articles/39723944605725-Capture-One-16-9-release-notes)
- [Color Editor 肤色统一](https://support.captureone.com/hc/en-us/articles/360002596077-Adjusting-skin-tones)
