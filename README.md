# AI 多语言视频本地化工具

这是一个 Python/Tkinter 桌面工具：Doubao Seed 2.0 Lite 负责视频理解、角色对应、对白时间轴、翻译和目标文化规划，Seedance 负责生成本地化画面、目标语言对白、口型、BGM、环境声和音效，并直接输出完整有声视频。

## 安装

要求：Windows 10/11 x64，首次启动时可以访问互联网。

```powershell
.\tools\install_dependencies.ps1
```

也可以直接双击根目录的 `start_app.cmd`。脚本会准备项目内 Python、Python 依赖和用于媒体信息检查的 ffprobe；运行时和下载缓存位于被 Git 忽略的目录，不要求系统级 Python、pip、Node.js 或其他 CLI。

复制 `.env.example` 为 `.env`，填写 Ark API Key 和当前账号控制台提供的 `SEEDANCE_MODEL_ID`。`DOUBAO_MODEL` 由应用固定为 `doubao-seed-2-0-lite-260428`，不要猜测或替换 Seedance Endpoint ID。`.env` 不应提交到 Git。也可以直接在桌面窗口填写 Ark API Key 和 Seedance Model/Endpoint ID；窗口关闭时会保存这些字段。

## 运行

```powershell
.\runtime\python3.13.15\python.exe app.py
```

窗口选择原视频、目标地区和可选人物/场景参考图。源语言由视频多模态分析自动识别。默认目标地区为 `Gulf (Arabic)`，对应 `ar-SA`。

## 处理链路

1. `analyzing`：ffprobe 检查视频并记录时长，将原视频和参考素材上传为临时 HTTPS Uguu URL；Doubao 一次完成视频理解、角色映射、对白时间轴、目标语言翻译和文化本地化规划，保存为 `json/localization_package.json`。
2. `generating_video`：根据 Localization Package 动态生成 Seedance Prompt，使用原视频和参考素材生成包含目标语言对白、BGM、环境声、音效和口型同步的完整有声视频。
3. 输出直接保存为 `output/final_<target_locale>.mp4`，不执行独立配音、音轨替换、混音、二次变速或二次 lip-sync。

每个任务的原始 Provider 响应保存在 `work/<job_id>/json/raw/`。任务使用 v3 checkpoint；旧链路 checkpoint 必须新建任务，不会回退到旧实现。Seedance 任务 ID 会在轮询前保存，失败后可使用“重新执行失败步骤”。

## 测试

```powershell
.\runtime\python3.13.15\python.exe -m unittest discover -s tests -v
.\runtime\python3.13.15\python.exe -m compileall -q .
```

单元测试使用 HTTP 和 Provider 替身，不会触发真实云端任务。真实验收需要已配置凭证和模型权限，并重点检查阿拉伯语、多角色对应、画外音、BGM/环境音/音效、口型同步以及最终文件同时包含视频和音频流。
