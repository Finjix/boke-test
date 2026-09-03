# MiniMax H3-Context-IR 视频本地化工具

这是一个仅使用 Python/Tkinter 和 MiniMax 的桌面视频本地化工具。处理链路为：

```text
源视频片段 + 目标地区
        ↓
MiniMax /v2/h3_context_ir
        ↓ 读取 task.content.prompt
MiniMax /v2/video_generation
        ↓ 读取 task.content.url
下载并校验生成视频
```

Context-IR 只负责根据源视频和目标地区生成增强提示词，不直接生成视频；普通 H3 使用该提示词和同一源视频片段生成结果。详见 [H3-Context-IR 文档](https://platform.minimax.cn/docs/api-reference/video-generation-v2-h3-context-ir) 和 [视频生成 V2 文档](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create)。

## 安装

要求：Windows 10/11 x64。首次启动安装项目运行时、ffprobe 和 Python 依赖：

```powershell
.\tools\install_dependencies.ps1
```

复制 `.env.example` 为 `.env`，填写 `MINIMAX_API_KEY`。也可以在 Tkinter 设置面板中填写，Key 会保存到项目根目录的本地设置文件，不会写入任务日志。

启动入口为：

```powershell
.\start_app.cmd
```

## 使用规则

- 目标地区从下拉框选择，例如 `Gulf (Arabic)`；语言和地区由 locale 预设派生，不接受自由文本。
- 源视频不超过 15 秒时，点击“开始处理”后立即执行一次 Context-IR 和一次 H3。
- 源视频超过 15 秒时，开始后只检查总时长并等待上传片段，不会把完整长视频发送到云端。
- 长视频由用户预先切成 3–15 秒片段；分片处理和本地拼接能力保留在流水线接口中，当前窗口不展示“上传下一片”和“完成拼接”按钮。
- MiniMax H3 的生成参数仍要求 4–15 秒；3 秒片段会在本地进入 H3 前补帧/补音到 4 秒，避免请求被接口拒绝。
- 所有已上传片段成功后，点击“完成拼接”使用本地 ffmpeg 按顺序合并。
- 单个上传片段按 MiniMax 官方视频输入限制校验，最大 50 MB；公网输入地址由 Uguu 临时提供。
- 任务只存在于当前 Tkinter 进程。失败后当前任务终止，需要重新开始；没有人工确认、重试、恢复、历史任务或命令行入口。
- 每个任务目录会保存当前会话、请求和原始响应，供排错使用；程序重启后不会读取这些文件来恢复任务。

## 配置

核心配置如下，完整示例见 `.env.example`：

```dotenv
MINIMAX_API_KEY=
MINIMAX_BASE_URL=https://api.minimax.cn
MINIMAX_RESOLUTION=768P
MINIMAX_TASK_TIMEOUT=7200
UGUU_UPLOAD_URL=https://uguu.se/upload
UGUU_MAX_FILE_MIB=50
UGUU_EXPIRE_HOURS=3
HTTP_TIMEOUT=180
POLL_INTERVAL=10
WORK_DIR=./work
FFPROBE_BIN=tools/ffmpeg/bin/ffprobe.exe
```

默认输出分辨率为 768P，可通过 `MINIMAX_RESOLUTION=2K` 调整；界面不提供分辨率选择。

## 测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m compileall -q .
git diff --check
```

离线测试使用 HTTP、媒体处理和上传替身，不创建真实云端任务。真实验收仍需使用有效 MiniMax Key、可访问的 Uguu 地址和实际视频片段。
