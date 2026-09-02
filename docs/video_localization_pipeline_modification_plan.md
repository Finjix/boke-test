# 多国视频本地化链路 v3

## 目标

将任意输入视频转换为指定国家/地区和语言的本地化有声视频，同时保留原始创意结构、镜头节奏、角色关系和主要动作。

## 最终架构

```text
原始视频 + 参考素材
        ↓
Doubao Seed 2.0 Lite
        ↓
Localization Package
        ↓
Seedance（画面 + 目标语言对白 + BGM + 环境声 + 音效 + 口型）
        ↓
最终有声本地化视频
```

Doubao 是导演/分析师，负责视频理解、源语言识别、角色映射、对白转写、时间定位、翻译、人物/场景本地化和文化要求规划。Seedance 是制片/生成器，负责根据原视频、动态 Prompt 和参考素材生成完整的最终画面与声音。

## Localization Package

Doubao 必须返回 JSON，顶层字段固定为：

```json
{
  "source": {"language": "en"},
  "target": {"language": "ar", "region": "Gulf", "locale": "ar-SA"},
  "video_analysis": {},
  "speakers": [
    {"id": "speaker_1", "visual_hint": "left person"}
  ],
  "dialogues": [
    {
      "speaker_id": "speaker_1",
      "start_ms": 1000,
      "end_ms": 2500,
      "source_text": "Hello",
      "target_text": "مرحبا"
    }
  ],
  "visual_localization": {},
  "cultural_requirements": []
}
```

`video_analysis` 至少应覆盖主题、故事结构、镜头结构、场景环境、人物关系、产品信息和核心创意。`visual_localization` 应覆盖人物、服饰、环境、建筑和道具。分析结果只描述可供 Seedance 执行的视觉和内容规划，不生成独立音轨。

本地校验必须确认：目标语言/地区/locale 与任务一致；speaker ID 唯一；每句对白引用已知 speaker；时间戳为非负整数且在视频时长内；对白有序、无重复、无异常大范围重叠；目标文本非空。格式错误时只允许同一 Doubao 模型带错误信息重试一次，不增加其他模型。

## Seedance 输入与输出

Seedance 内容包含：

- 动态生成的本地化 Prompt；
- 原始视频 HTTPS URL；
- 可选人物和场景参考图片 HTTPS URL。

Prompt 必须要求：

- 保持原故事、镜头构图、运镜、节奏、动作和角色关系；
- 应用 Localization Package 中的人物、服饰、场景、建筑、道具和文化要求；
- 使用目标语言生成自然对白，并保持 speaker 对应和口型同步；
- 同时生成 BGM、环境声和重要音效；
- 不增加对白、不交换角色、不添加字幕、不依赖后处理。

任务请求固定启用 `generate_audio: true`。成功结果中的 `content.video_url` 直接下载为 `output/final_<locale>.mp4`，再由 ffprobe 验证同时包含视频和音频流。禁止单独提取音频、生成独立音轨、音轨替换、混音、变速或二次口型同步。

## Pipeline 与状态

公开阶段只有：

```text
analyzing → generating_video → completed
```

失败统一进入 `failed`，记录当前阶段、Provider、请求 ID、错误码和原始响应路径。任务指标包括 `source_video_duration`、`analysis_duration`、`seedance_duration`、`total_duration`、`speaker_count` 和 `dialogue_count`。

Pipeline 版本为 3。v2 及更早 checkpoint 不迁移、不回退，必须新建任务。缓存键包含源视频哈希、目标语言/地区/locale、Doubao 模型、Seedance 模型和两个 Prompt 版本。

## 语言与验收

首阶段确保支持 English、中文、日本語、한국어、Español、Français、Deutsch、Português、Русский 和 العربية，并以 `ar-SA` 作为重点验收 locale；已有额外语言选项继续保留。

验收覆盖单角色、双角色交替、三角色、画外音、带 BGM/环境声/音效以及目标语言时长差异明显的素材。除单元测试和 mock Pipeline 外，必须使用真实视频检查最终画面、目标语言对白、角色对应、文化本地化、音频完整性和口型同步。
