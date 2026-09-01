# AI 多语言视频本地化工具

这是一个 Python/Tkinter 桌面工具，按 `D:\AI_video_localization_plan.md` 的首版方案完成视频对白本地化：MediaKit 负责分离人声和 ASR，Doubao 负责角色分析与翻译，Seed-Audio 1.0 生成干声对白，Seedance 生成本地化画面，FFmpeg 恢复原始背景音并封装最终视频。

## 安装

要求：

- Python 3.11 或更高版本
- Node.js 18 或更高版本（仅用于本地 MediaKit 包管理）

```powershell
.\tools\install_dependencies.ps1
```

安装脚本会创建 `.venv`、将 Python 依赖安装到项目内，下载并校验项目内的
FFmpeg/FFprobe，并将 MediaKit CLI 放到 `tools\mediakit\mediakit-cli.exe`。
程序会自动优先使用这些本地工具，不依赖系统级 pip 或全局 MediaKit CLI。
FFmpeg Windows 构建入口见
[FFmpeg 官方下载页](https://ffmpeg.org/download.html)，MediaKit CLI 使用
[官方仓库](https://github.com/volcengine/mediakit-cli)。

配置 MediaKit：

```powershell
.\tools\mediakit\mediakit-cli.exe init --mode cloud-first --api-key YOUR_MEDIAKIT_API_KEY --yes
.\tools\mediakit\mediakit-cli.exe doctor
.\tools\mediakit\mediakit-cli.exe version
```

复制 `.env.example` 为 `.env`，填写 Ark、MediaKit、Seed-Audio Key 和当前账号控制台提供的 `SEEDANCE_MODEL_ID`。`.env` 不应提交到 Git。

## 运行

双击根目录的 `start_app.cmd`，或在 PowerShell 中执行：

```powershell
.\.venv\Scripts\python.exe app.py
```

点击“开始”后会先执行 Preflight。任何依赖、凭证、模型权限或 Uguu 文件大小检查失败，正式 Pipeline 都不会启动。

## 处理链路

1. ffprobe 检查输入并拒绝超过配置上限的视频。
2. MediaKit 分离 `voice.wav` 和 `background.wav`。
3. MediaKit 对人声执行 ASR 与 Speaker Diarization。
4. 抽取说话时段关键帧，上传 Uguu 后交给 Doubao 分析角色。
5. Doubao 一次翻译完整时间线。
6. Seed-Audio 1.0 只生成干声对白，并执行时长校验。
7. 上传 Seedance 所需的输入视频、目标对白和可选参考图。
8. Seedance 画面完成后丢弃其音轨。
9. FFmpeg 混合原始背景音与目标对白，输出 `work/<job_id>/output/final.mp4`。

所有 Provider 原始响应保存在 `work/<job_id>/json/raw/`。应用不包含传统 TTS、备用音频模型、声音克隆或其他 Provider。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q .
```

单元测试使用 HTTP 和 subprocess 测试替身，不会触发真实云端任务。真实视频验收仍需在依赖、凭证和模型权限就绪后手动执行，并检查最终画面、口型、背景音和音轨映射。
