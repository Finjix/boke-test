# AI 多语言视频本地化工具

这是一个 Python/Tkinter 桌面工具，使用 Doubao Seed 2.0 Lite 260428 分析视频并生成结构化本地化脚本，使用 Seed Audio 1.0 生成包含对白、BGM、环境声和重要音效的完整目标音频，再由 Seedance 生成画面，最后用 FFmpeg 封装同一份目标音频。

## 安装

要求：

- Windows 10/11 x64
- 首次启动时可以访问互联网

```powershell
.\tools\install_dependencies.ps1
```

也可以直接双击根目录的 `start_app.cmd`，脚本会在首次启动时自动准备项目内 Python、Python 依赖和 FFmpeg/FFprobe。运行时和下载缓存位于被 Git 忽略的目录，不要求系统级 Python、pip、Node.js 或其他 CLI。

复制 `.env.example` 为 `.env`，填写 Ark、Seed Audio Key 和当前账号控制台提供的 `SEEDANCE_MODEL_ID`。`DOUBAO_MODEL` 与 `SEED_AUDIO_MODEL` 由应用固定校验，不要改成其他模型。`.env` 不应提交到 Git。也可以直接在桌面窗口填写 API Key 和 Seedance Model/Endpoint ID；窗口关闭时会将这些字段保存到项目根目录的 `.video-localizer-settings.json`，下次启动自动恢复。

## 运行

双击根目录的 `start_app.cmd`，或在 PowerShell 中执行：

```powershell
.\runtime\python3.13.15\python.exe app.py
```

窗口只选择目标地区，例如 `Gulf (Arabic)` 或 `Japan (Japanese)`。源语言由视频多模态分析自动识别。点击“开始”后先执行不产生模型内容的 Preflight；凭证、模型配置、FFmpeg/FFprobe、Uguu 和 Seedance 检查通过后才进入正式任务。

## 处理链路

1. `analyzing`：ffprobe 检查输入，提取完整原始音频 `audio/original_audio.wav`，将视频和原始音频上传为临时 HTTPS Uguu URL，并让 Doubao 一次完成源语言识别、角色对应、对白时间轴和目标语言翻译。
2. `generating_audio`：以完整原始音频作为唯一参考，Seed Audio 1.0 生成一次完整目标音频 `audio/localized_audio.wav`，保留对白表演、BGM、环境声和重要音效。
3. `generating_video`：将原视频、原始音频、目标音频和可选参考图上传给 Seedance，关闭自身音频生成（`generate_audio=false`），生成后丢弃 Seedance 音轨。
4. `muxing`：FFmpeg 复制 Seedance 视频流并编码同一份本地目标音频，输出 `output/final_<target_locale>.mp4`，例如 `final_ar-SA.mp4`。

每个任务的原始响应保存在 `work/<job_id>/json/raw/`，结构化分析保存在 `json/analysis.json`。旧版 checkpoint 不会回退到旧链路；旧版任务需要新建任务，v2 失败任务可使用“重新执行失败步骤”。

## 测试

```powershell
.\runtime\python3.13.15\python.exe -m unittest discover -s tests -v
.\runtime\python3.13.15\python.exe -m compileall -q .
```

单元测试使用 HTTP 和外部 Provider 替身，不会触发真实云端任务。真实视频验收仍需在依赖、凭证和模型权限就绪后手动执行，并检查多角色对应、画外音、BGM/音效、语言时长差异、口型同步和最终音轨映射。
