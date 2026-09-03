# Doubao Seed + MiniMax H3 视频本地化工具

这是一个 Python/Tkinter 桌面工具。Doubao Seed 2.0 Lite 先完整分析源视频，输出包含人物、场景、分镜、可见文字、对白和语音要求的本地化方案；随后使用源视频关键帧调用 Doubao Seedream 5.0 Pro，为每个分镜生成目标场景参考图，并将前一张已生成参考图作为连续性参考；最后由 MiniMax H3 根据方案和分镜参考图生成目标地区版本。源视频始终是内容、镜头和节奏的核心，前端不再接收用户参考图。

## 安装

要求：Windows 10/11 x64。首次启动需要安装依赖并访问网络：

```powershell
.\tools\install_dependencies.ps1
```

复制 `.env.example` 为 `.env`，填写 `ARK_API_KEY` 和 `MINIMAX_API_KEY`。Doubao 模型固定为 `doubao-seed-2-0-lite-260428`，H3 模型固定为 `MiniMax-H3`。API Key 也可以在桌面设置中填写，保存到项目根目录的本地设置文件；不会写入任务日志。

官方规格与异步接口说明见 [MiniMax 视频生成文档](https://platform.minimaxi.com/docs/guides/video-generation)。项目默认使用 768P；如需 2K，才在环境变量中显式设置。H3 的视频生成结果仍需人工验收，提示词和参考素材不能保证像素级人物/场景一致，也不能保证任何内容审核一定通过。

## 处理规则

- 所有任务先将完整源视频交给 Doubao 分析一次；分析结果会保存到本地。
- 默认在 Doubao 完成后等待人工确认；确认后才创建 H3 任务。设置中可开启自动继续。
- 源视频不超过 15 秒：确认后创建一个 H3 转化任务。
- 源视频超过 15 秒：Doubao 分析完成后等待上传片段，不会自动创建 H3 任务。片段必须按顺序上传，每片 4–15 秒。
- Doubao 方案要求覆盖人物、服装、场景、建筑、道具、车辆、可见文字/招牌/包装、目标语音和对白时间轴，同时保持人物关系、创意结构、镜头构图、动作节奏、转场、剪辑节奏和整体效果一致。
- Doubao 为每个检测到的镜头选择一个关键帧和连续性分组；每个镜头调用一次 Seedream 5.0 Pro，以源关键帧为当前镜头输入，并从第二个镜头开始附带前一张已生成参考图作为连续性参考，生成最低 `2K` 目标场景参考图（用于低成本分镜，不是最终交付分辨率）。
- H3 每次最多接收 9 张 Seedream 分镜参考图；长视频片段按源视频时间轴映射到对应分镜。若一片覆盖超过 9 个镜头，程序会在创建 H3 task 前要求重新切分。
- 后续 H3 片段不再使用上一片生成视频或原始均匀帧作为背景参考，仍以当前手动上传的源片段为运动、镜头和剪辑依据，并使用 Doubao 分析生成的 Seedream 分镜图保持场景连续。
- H3 只开放官方稳定对白语言集合：Arabic、Chinese、English、French、German、Italian、Japanese、Korean、Portuguese、Russian、Spanish。地区和 locale 会进入提示词，但 H3 没有具体国家方言保证。
- 全部片段完成后执行本地拼接。每个片段的原始输入、上传记录、请求响应、task ID、失败原因、Provider 原始输出和本地标准化输出都单独保存。H3 返回的封装时长可能带有编码误差，程序会在本地将成功下载的结果裁剪/补帧到请求的整数秒，不会因此重复付费创建任务。

## 脚本操作

```powershell
# 新任务；默认在 Doubao 分析完成后等待人工确认
.\runtime\python3.13.15\python.exe h3_workflow.py start --video .\case\测试.mp4 --language ar --region "Saudi Arabia" --locale ar-SA

# 如需自动跳过 Doubao 方案和 Seedream 参考图确认并进入 H3
.\runtime\python3.13.15\python.exe h3_workflow.py start --auto-continue --video .\case\测试.mp4 --language ar --region "Saudi Arabia" --locale ar-SA

# 查看 Doubao 方案后确认并生成 Seedream 分镜参考图
.\runtime\python3.13.15\python.exe h3_workflow.py approve-doubao --job-id <job_id>
.\runtime\python3.13.15\python.exe h3_workflow.py retry-doubao --job-id <job_id>

# 查看 Seedream 分镜图后确认进入 H3，或只重试失败的分镜图
.\runtime\python3.13.15\python.exe h3_workflow.py approve-seedream --job-id <job_id>
.\runtime\python3.13.15\python.exe h3_workflow.py retry-seedream --job-id <job_id> --shot-id shot_001

# 长视频按顺序追加片段
.\runtime\python3.13.15\python.exe h3_workflow.py append-segment --job-id <job_id> --video .\片段01.mp4
.\runtime\python3.13.15\python.exe h3_workflow.py append-segment --job-id <job_id> --video .\片段02.mp4

# 轮询中断后继续原 task；失败后只重试当前片段
.\runtime\python3.13.15\python.exe h3_workflow.py continue --job-id <job_id>
.\runtime\python3.13.15\python.exe h3_workflow.py retry --job-id <job_id>
.\runtime\python3.13.15\python.exe h3_workflow.py finish --job-id <job_id>

# 只读取本地历史，不调用网络
.\runtime\python3.13.15\python.exe h3_workflow.py history
```

## 本地持久化

每个任务的事实来源是 `work/<job_id>/checkpoint.json`，快速索引是 `work/history.json`。Doubao 节点保存在 `json/nodes/doubao/attempt_<n>/`，包括原始响应、校验后的 package、H3 提示词和失败记录；Seedream 每个镜头保存在 `json/nodes/seedream/shot_<shot_id>/attempt_<n>/`，包括请求、原始响应、源关键帧、Provider 输出、规范化参考图和失败记录；H3 每次尝试保存在 `json/nodes/h3/segment_<n>/attempt_<n>/`，包括 `content.json`、原始响应、最终响应、失败记录和输出文件。应用重启时只读取历史，不自动重新调用 Doubao、Seedream、轮询或创建 H3 任务；明确确认、继续或重试后才访问云端。v4/v5/v6 checkpoint 仍可在历史中查看，但不能直接恢复。

## 运行与测试

```powershell
.\runtime\python3.13.15\python.exe app.py
.\runtime\python3.13.15\python.exe -m unittest discover -s tests -v
.\runtime\python3.13.15\python.exe -m compileall -q .
git diff --check
```

单元测试使用 Provider 和 HTTP 替身，不会创建真实 H3 任务。真实验收应重点检查目标语言、人物/场景连续性、镜头节奏、音频流和审核结果。
