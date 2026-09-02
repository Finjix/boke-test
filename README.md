# MiniMax H3 视频转化工具

这是一个 Python/Tkinter 桌面工具，使用 MiniMax H3 直接将源视频转化为目标地区版本。活动链路不再调用 Doubao，也不提供用户参考视频上传：源视频始终是唯一的内容与镜头核心，用户参考图只用于风格或外观提示。

## 安装

要求：Windows 10/11 x64。首次启动需要安装依赖并访问网络：

```powershell
.\tools\install_dependencies.ps1
```

复制 `.env.example` 为 `.env`，填写 `MINIMAX_API_KEY`。模型固定为 `MiniMax-H3`，国内端点固定默认值 `https://api.minimax.cn`。API Key 也可以在桌面设置中填写，保存到项目根目录的本地设置文件；不会写入任务日志。

官方规格与异步接口说明见 [MiniMax 视频生成文档](https://platform.minimaxi.com/docs/guides/video-generation)。H3 的视频生成结果仍需人工验收，提示词和参考素材不能保证像素级人物/场景一致，也不能保证任何内容审核一定通过。

## 处理规则

- 源视频不超过 15 秒：直接创建一个 H3 转化任务。
- 源视频超过 15 秒：只建立本地任务并提示上传片段，不会自动创建 H3 任务。片段必须按顺序上传，每片 4–15 秒。
- 第一片使用当前原片和用户参考图。
- 后续片段使用当前原片作为内容核心，并在 H3 参考视频总时长允许时附带上一片生成结果，保持人物、场景和风格连续。若当前片段加上一片超过 H3 的 15 秒参考视频总时长限制，则使用原始主视频均匀抽取的 4 张一致性参考帧；这时用户参考图最多保留 5 张，系统不会静默丢弃第 6–9 张。
- H3 只开放官方稳定对白语言集合：Arabic、Chinese、English、French、German、Italian、Japanese、Korean、Portuguese、Russian、Spanish。地区和 locale 会进入提示词，但 H3 没有具体国家方言保证。
- 全部片段完成后执行本地拼接。每个片段的原始输入、上传记录、请求响应、task ID、失败原因和输出都单独保存。

## 脚本操作

```powershell
# 新任务；短视频会直接创建 H3，长视频返回 job_id 并等待片段
.\.venv\Scripts\python.exe h3_workflow.py start --video .\case\测试.mp4 --language ar --region "Saudi Arabia" --locale ar-SA

# 长视频按顺序追加片段
.\.venv\Scripts\python.exe h3_workflow.py append-segment --job-id <job_id> --video .\片段01.mp4
.\.venv\Scripts\python.exe h3_workflow.py append-segment --job-id <job_id> --video .\片段02.mp4

# 轮询中断后继续原 task；失败后只重试当前片段
.\.venv\Scripts\python.exe h3_workflow.py continue --job-id <job_id>
.\.venv\Scripts\python.exe h3_workflow.py retry --job-id <job_id>
.\.venv\Scripts\python.exe h3_workflow.py finish --job-id <job_id>

# 只读取本地历史，不调用网络
.\.venv\Scripts\python.exe h3_workflow.py history
```

## 本地持久化

每个任务的事实来源是 `work/<job_id>/checkpoint.json`，快速索引是 `work/history.json`。H3 每次尝试保存在 `json/nodes/h3/segment_<n>/attempt_<n>/`，包括 `content.json`、原始响应、最终响应、失败记录和输出文件。应用重启时只读取历史，不自动轮询或创建任务；明确点击继续或重试后才访问 H3。

## 运行与测试

```powershell
.\runtime\python3.13.15\python.exe app.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q .
git diff --check
```

单元测试使用 Provider 和 HTTP 替身，不会创建真实 H3 任务。真实验收应重点检查目标语言、人物/场景连续性、镜头节奏、音频流和审核结果。
