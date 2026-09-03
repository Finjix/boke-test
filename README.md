# MiniMax H3 视频处理工具

这是一个 Windows 桌面工具，使用 Tkinter 调用 MiniMax H3：

    单个视频（3–15 秒）
            ↓
    可选人物参考图、场景参考图
            ↓
    目标地区
            ↓
    H3-Context-IR 输出结构提示词
            ↓
    MiniMax-H3 生成视频
            ↓
    output/YYYYMMDD_HHMMSS.mp4

## 使用

双击 start_app.cmd。首次启动会自动准备项目内 Python、FFmpeg 和依赖。

在窗口中填写 MiniMax API Key，选择视频、可选参考图和目标地区，然后点击“开始处理”。

- 视频原始时长必须为 3–15 秒。
- 3 秒视频按 4 秒提交给 H3，结果保留 4 秒。
- 输入视频会临时转为 H.264 MP4。
- 参考素材以内嵌 Base64 方式发送；请求体超过 MiniMax 64 MB 限制时会直接报错。
- 结果只保存到项目根目录的 output 文件夹。
- 任务结束后，临时 work 文件会自动清理，不保存历史、日志或任务恢复记录。

## 配置

Key 可以在 GUI 保存，也可以复制 .env.example 为 .env 后填写 MINIMAX_API_KEY。
分辨率默认为 768P，可在 .env 中改为 2K；GUI 不提供其他高级配置。

## 测试

    runtime\python3.13.15\python.exe -m unittest discover -s tests -v
    runtime\python3.13.15\python.exe -m compileall -q .
    git diff --check

真实接口验收需要有效的 MiniMax API Key。MiniMax 文档列出的多模态内容类型和任务结果字段以官方页面为准。
