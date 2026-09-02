# 多国视频本地化链路修改计划

## 1. 项目目标

本项目用于将任意输入视频自动转换为多个国家和语言版本。

本次修改的核心目标是简化音频处理链路，减少模型调用次数和传统音频后期步骤。新的处理流程使用 Doubao Seed 2.0 Lite 直接对原视频进行音视频联合理解，一次完成对白识别、角色对应、时间定位和目标语言翻译，再使用 Seed Audio 1.0 基于原始完整音频生成目标语言完整音轨，最后将该音轨作为 Seedance 的音频条件生成本地化画面和对应口型。

不再要求原始 BGM、环境声和音效严格无损保留，允许 Seed Audio 1.0 根据参考音频进行整体重建，以换取更短的处理链路、更低的工程复杂度和更低的成本。

---

## 2. 最终链路

```text
原始视频
   ↓
Doubao Seed 2.0 Lite
   ↓
音视频联合理解
   ├─ 识别视频中的说话角色
   ├─ 判断每句对白属于哪个角色
   ├─ 获取对白开始/结束时间
   ├─ 识别原始对白内容
   └─ 直接翻译为目标语言
   ↓
结构化目标语言角色脚本
   ↓
Seed Audio 1.0
   ├─ 原始完整音频作为 Reference Audio
   ├─ 目标语言角色脚本
   └─ 对白时间信息
   ↓
目标语言完整音轨
   ├─ 目标语言对白
   ├─ 角色声音关系
   ├─ 表演情绪
   ├─ BGM
   ├─ 环境声
   └─ 主要音效
   ↓
Seedance
   ├─ 原始视频
   ├─ Seed Audio 1.0 最终音轨
   ├─ 人物 / 场景本地化要求
   └─ Uguu 参考素材
   ↓
本地化画面 + 目标语言口型
   ↓
FFmpeg
   ├─ Seedance 最终画面
   └─ Seed Audio 1.0 最终音轨
   ↓
最终本地化视频
```

核心模型链路：

```text
Doubao Seed 2.0 Lite
        ↓
Seed Audio 1.0
        ↓
Seedance
```

---

## 3. 删除旧音频处理链路

删除以下模块及相关调用逻辑：

- 独立 ASR 模型调用
- 独立翻译模型调用
- 独立 Speaker Diarization 模型调用
- 人声 / 背景音分离
- Vocal Stem 生成
- Background Stem 生成
- BGM 单独提取
- 环境声单独提取
- 音效单独提取
- 角色性别分析
- 角色年龄分析
- 角色音色标签分析
- 独立情绪识别
- 每角色单独 TTS
- 多段语音拼接
- 原背景音重新混音
- Ducking
- 多轨 Loudness 匹配
- 原音效严格保真逻辑
- 原始音频 Stem 缓存逻辑

如果项目中存在类似以下中间文件，应取消作为主链路依赖：

```text
vocals.wav
background.wav
music.wav
sfx.wav
speaker_*.wav
dialogue_mix.wav
```

新的音频主链路只保留：

```text
original_audio.*
localized_audio.*
```

---

## 4. Doubao Seed 2.0 Lite

### 4.1 模型

使用：

```text
doubao-seed-2-0-lite-260428
```

不要使用旧版不支持完整音频理解能力的 Lite 版本。

### 4.2 输入

优先直接输入原始视频，而不是仅输入抽取后的音频。

原因是角色识别需要同时利用：

- 视频画面中的人物
- 人物嘴部运动
- 声音
- 对白时间
- 镜头切换
- 画外音关系

输入参数至少包括：

```text
source_video
target_language
```

### 4.3 职责

一次调用完成：

1. 判断源语言。
2. 识别所有有效对白。
3. 判断视频中有多少个持续存在的说话角色。
4. 为每个角色建立稳定的 `speaker_id`。
5. 判断每句对白属于哪个 `speaker_id`。
6. 获取每句对白的开始时间和结束时间。
7. 转写原始对白。
8. 直接翻译成目标语言。
9. 在目标语言存在性别、敬语或身份差异时，结合画面和上下文生成正确表达。
10. 保持多句对白之间的语义连续性。

不要求它生成：

- 音色描述
- 年龄标签
- 性别标签
- 声纹 embedding
- BGM 描述
- 音效描述
- 环境声描述

这些信息由 Seed Audio 1.0 直接从 Reference Audio 中获取。

---

## 5. 音频理解输出结构

统一输出以下 JSON：

```json
{
  "source_language": "en",
  "target_language": "ar",
  "speakers": [
    {
      "id": "speaker_1",
      "visual_hint": "left person"
    },
    {
      "id": "speaker_2",
      "visual_hint": "right person"
    }
  ],
  "dialogues": [
    {
      "speaker_id": "speaker_1",
      "start_ms": 1200,
      "end_ms": 2850,
      "source_text": "Where are you going?",
      "target_text": "إلى أين أنت ذاهب؟"
    },
    {
      "speaker_id": "speaker_2",
      "start_ms": 3100,
      "end_ms": 4620,
      "source_text": "I'm going home.",
      "target_text": "أنا ذاهبة إلى المنزل."
    }
  ]
}
```

### 字段说明

`source_language`

原始对白语言。

`target_language`

当前生成任务的目标语言。

`speakers`

只负责建立角色身份，不负责定义声音。

`visual_hint`

用于辅助后续调试角色映射，可使用：

```text
left person
right person
foreground person
background person
off-screen narrator
```

禁止将 `visual_hint` 作为音色生成条件。

`start_ms / end_ms`

使用整数毫秒，避免浮点时间误差。

`source_text`

保留用于：

- 调试
- UI 展示
- 翻译质量检查
- 日志

不作为 Seed Audio 1.0 的主要生成内容。

`target_text`

Seed Audio 1.0 实际需要生成的目标语言对白。

---

## 6. Doubao Seed 2.0 Lite Prompt

系统提示词应明确要求模型只输出结构化 JSON。

核心要求：

```text
Analyze the complete video using both visual and audio information.

Identify every speaking character and maintain a stable speaker ID
for the same character throughout the video.

Determine which character speaks each dialogue line using both
voice information and visible speaking behavior.

Transcribe the original dialogue and translate it directly into
the requested target language.

Preserve the original meaning, tone, conversational relationship,
and approximate dialogue duration.

Return dialogue start and end timestamps in milliseconds.

Do not describe voice timbre, music, ambience, or sound effects.

Return valid JSON only.
```

同时通过 JSON Schema 或等效结构化输出能力限制返回格式。

---

## 7. 结构化结果校验

Doubao Seed 2.0 Lite 返回结果后执行轻量校验。

检查：

- JSON 可解析
- `speaker_id` 均存在
- `start_ms < end_ms`
- 时间不能超出视频总时长
- Dialogue 按时间递增
- `target_text` 不为空
- 同一角色必须保持相同 `speaker_id`
- 不允许明显重复 Dialogue
- 不允许时间段出现异常大范围重叠

如果结构格式不合法：

```text
同一模型重新请求一次
```

重新请求时附带格式错误信息，让模型修正 JSON。

不调用额外 ASR、翻译或 Speaker 模型。

---

## 8. Seed Audio 1.0

### 8.1 定位

Seed Audio 1.0 不作为传统 TTS 使用。

它负责根据原始音频 Reference 重建整个目标语言声音场景。

### 8.2 输入

输入：

```text
1. original_audio
2. target dialogue timeline
3. speaker assignment
4. generation instruction
```

其中 `original_audio` 直接从原视频提取完整音轨。

不要进行：

```text
人声分离
背景分离
BGM 分离
SFX 分离
```

### 8.3 目标脚本

根据 Doubao Seed 2.0 Lite 的结果构造：

```text
[00:01.200 - 00:02.850] speaker_1:
إلى أين أنت ذاهب؟

[00:03.100 - 00:04.620] speaker_2:
أنا ذاهبة إلى المنزل.
```

角色 ID 必须保持与分析结果一致。

---

## 9. Seed Audio 1.0 生成要求

核心 Prompt：

```text
Use the original audio as the reference for the complete sound scene.

Generate a localized version in the target language.

Preserve the identity and relationship of each original speaker.
Each translated dialogue line must be spoken by its corresponding
speaker from the reference audio.

Preserve the original performance style, emotion, conversational
rhythm and approximate dialogue timing.

Recreate the surrounding sound scene based on the reference audio,
including background music, ambience and important sound effects.

Do not add new dialogue.
Do not swap speakers.
Do not translate non-speech sound events.
```

允许：

- BGM 与原片存在轻微差异
- 环境声存在轻微差异
- Foley / SFX 存在轻微变化
- 声场细节重新生成
- 新语言对白时长产生合理变化

优先保证：

1. 台词内容正确
2. 角色对应正确
3. 多角色不串声
4. 情绪自然
5. 对白时间接近原视频
6. 整体声音场景自然

不再要求 sample-level 原音效保真。

---

## 10. Seed Audio 输出

输出统一命名：

```text
localized_audio.<ext>
```

该文件是当前语言版本的最终音轨。

后续所有视频生成与封装都使用同一份文件。

禁止 Seedance 完成后再次重新生成音频。

---

## 11. Seedance 调用顺序

必须保证：

```text
Seed Audio 1.0
        ↓
Seedance
```

不能：

```text
Seedance
   ↓
Seed Audio 1.0
```

原因是 Seedance 需要基于最终生成的真实目标语言音频建立口型。

### Seedance 输入

```text
source_video
localized_audio
localization_prompt
reference_assets
```

其中：

`source_video`

原始视频。

`localized_audio`

Seed Audio 1.0 最终输出音轨。

`localization_prompt`

描述：

- 目标国家 / 地区
- 人物本地化
- 场景本地化
- 服饰 / 道具 / 建筑等视觉变化
- 保持原始创意结构
- 保持镜头节奏
- 保持镜头运动
- 保持人物动作
- 根据输入音频同步人物口型

`reference_assets`

继续使用 Uguu 存储的人物和场景参考素材 URL。

本次修改不更换项目现有 Seedance 模型配置。

---

## 12. 口型同步

Seedance 必须使用：

```text
localized_audio
```

作为音频条件。

口型同步的基准不是：

```text
翻译文本
```

也不是：

```text
原始音频
```

而是：

```text
Seed Audio 1.0 最终生成的 localized_audio
```

因此完整依赖关系为：

```text
target_text
    ↓
Seed Audio 1.0
    ↓
localized_audio
    ↓
Seedance
    ↓
lip sync
```

这样能够避免翻译文字预计时长与实际语音时长不同导致的口型漂移。

---

## 13. 最终 FFmpeg 封装

Seedance 生成完成后，将：

```text
Seedance video stream
+
localized_audio
```

进行最终封装。

要求：

- 视频流优先直接 copy
- 音频根据目标容器要求编码
- 不执行二次变速
- 不重新进行音频时间拉伸
- 不修改音频起点
- 保证 `localized_audio` 与 Seedance 输入的是同一份文件

最终：

```text
final_<locale>.mp4
```

例如：

```text
final_ar-SA.mp4
final_ja-JP.mp4
final_ko-KR.mp4
final_de-DE.mp4
```

---

## 14. 多语言任务复用

一个原视频只需要执行一次源视频语义分析时，可以将不依赖目标语言的角色信息缓存。

推荐逻辑：

```text
原视频
 ↓
基础音视频分析
 ↓
角色关系 / 原始对白 / 时间轴
 ↓
针对不同 target_language 生成翻译结果
```

如果当前 Doubao Seed 2.0 Lite 接口调用方式不方便将分析和翻译拆成内部缓存，则可以针对每个目标语言直接执行一次完整调用，优先保证实现简单。

不要为了减少一次 Lite 调用重新引入额外翻译模型。

---

## 15. 建议的任务数据结构

```json
{
  "job_id": "xxx",
  "source_video_url": "...",
  "source_audio_path": "...",
  "target_locale": "ar-SA",

  "analysis": {
    "source_language": "en",
    "speakers": [],
    "dialogues": []
  },

  "audio": {
    "status": "pending",
    "localized_audio_path": null
  },

  "video": {
    "status": "pending",
    "seedance_task_id": null,
    "generated_video_path": null
  },

  "output": {
    "status": "pending",
    "final_video_path": null
  }
}
```

---

## 16. 推荐代码模块

将主流程收敛成：

```text
src/
  localization/
    analyze-video.*
    generate-audio.*
    generate-video.*
    mux-output.*
    pipeline.*
```

职责：

### `analyze-video`

调用 Doubao Seed 2.0 Lite。

输入：

```text
video
target_language
```

输出：

```text
LocalizationScript
```

### `generate-audio`

调用 Seed Audio 1.0。

输入：

```text
original_audio
LocalizationScript
```

输出：

```text
localized_audio
```

### `generate-video`

调用 Seedance。

输入：

```text
source_video
localized_audio
localization_config
reference_assets
```

输出：

```text
generated_video
```

### `mux-output`

输入：

```text
generated_video
localized_audio
```

输出：

```text
final_video
```

### `pipeline`

编排：

```text
analyzeVideo
    ↓
generateAudio
    ↓
generateVideo
    ↓
muxOutput
```

---

## 17. Pipeline 伪代码

```ts
async function localizeVideo(input: LocalizeVideoInput) {
  const sourceAudio = await extractOriginalAudio(input.video);

  const script = await analyzeAndTranslateVideo({
    video: input.video,
    targetLanguage: input.targetLanguage,
  });

  validateLocalizationScript(script);

  const localizedAudio = await generateLocalizedAudio({
    referenceAudio: sourceAudio,
    script,
  });

  const localizedVideo = await generateLocalizedVideo({
    sourceVideo: input.video,
    audio: localizedAudio,
    targetRegion: input.targetRegion,
    referenceAssets: input.referenceAssets,
  });

  const finalVideo = await muxFinalOutput({
    video: localizedVideo,
    audio: localizedAudio,
  });

  return finalVideo;
}
```

主链路中不得重新插入：

```text
ASR
Translation LLM
Speaker Diarization
Audio Separation
TTS
Audio Mixing
```

---

## 18. 旧代码迁移

Codex 修改时先检查现有项目中以下模块：

```text
asr
transcribe
translate
speaker
diarization
separation
demucs
vocal
background
tts
voice
mix
ducking
loudness
```

如果这些模块只服务于旧音频链路：

1. 移除主 Pipeline 引用。
2. 删除无用配置项。
3. 删除无用环境变量。
4. 删除对应 API Client。
5. 删除无用中间文件类型。
6. 删除相关状态字段。
7. 更新前端进度状态。
8. 更新日志名称。
9. 更新 README / 项目说明。

不要保留旧链路作为 fallback。

---

## 19. 前端任务阶段

前端不再显示大量音频处理阶段。

旧：

```text
提取音频
→ 分离人声
→ ASR
→ 角色识别
→ 翻译
→ 配音
→ 拼接
→ 混音
→ 视频生成
→ 合成
```

改为：

```text
分析视频
→ 生成本地化音频
→ 生成本地化视频
→ 最终合成
```

建议状态：

```ts
type LocalizationStage =
  | "analyzing"
  | "generating_audio"
  | "generating_video"
  | "muxing"
  | "completed"
  | "failed";
```

---

## 20. 日志

每个任务至少记录：

```text
job_id
target_locale
source_video_duration
analysis_duration
seed_audio_duration
seedance_duration
mux_duration
total_duration
speaker_count
dialogue_count
```

保存 Doubao Seed 2.0 Lite 的结构化输出用于调试。

不要保存额外的人声音轨 Stem。

---

## 21. 缓存

允许缓存：

```text
source video
source audio
analysis JSON
localized audio
Seedance result
final output
```

缓存键至少包含：

```text
source_video_hash
target_locale
model_version
prompt_version
```

Seed Audio 和 Seedance Prompt 变化时必须使旧缓存失效。

---

## 22. 失败处理

不增加其它模型作为 fallback。

允许：

### Doubao Seed 2.0 Lite

- 网络失败重试
- 限流重试
- JSON 格式错误时使用同一模型修正一次

### Seed Audio 1.0

- 网络失败重试
- 任务失败重试

### Seedance

- 网络失败重试
- 任务失败重试

如果模型连续失败：

```text
任务标记 failed
返回明确错误
```

不要自动切换：

- 其它 ASR
- 其它翻译模型
- 其它 TTS
- 其它视频生成模型

---

## 23. 验收测试

至少准备以下测试素材：

### Test A：单角色

```text
1 人
连续对白
有 BGM
```

检查：

- 翻译正确
- 声音自然
- 口型同步

### Test B：双角色交替对白

```text
角色 A
角色 B
角色 A
角色 B
```

检查：

- speaker 不串
- A 始终保持 A
- B 始终保持 B

### Test C：三角色

检查：

- 角色数量正确
- 每句话归属正确
- Seed Audio 不出现角色交换

### Test D：画外音

检查：

- 能识别 narrator
- 不错误绑定到画面人物

### Test E：带明显音效和 BGM

检查：

- Seed Audio 输出仍然是完整音轨
- 无需额外 background mix
- 主要声音事件基本存在

### Test F：目标语言时长差异明显

例如：

```text
English → German
English → Arabic
English → Japanese
```

检查：

- Seed Audio 能合理调整对白节奏
- Seedance 使用真实最终音频生成口型
- 最终 FFmpeg 不产生二次音画偏移

---

## 24. 验收标准

完成本次修改后必须满足：

- 主流程只有三个核心模型阶段：
  - Doubao Seed 2.0 Lite
  - Seed Audio 1.0
  - Seedance
- 不再单独调用 ASR。
- 不再单独调用翻译模型。
- 不再调用 Speaker Diarization 专用模型。
- 不再做人声 / 背景分离。
- 不再执行传统多轨混音。
- Seed Audio 只调用一次生成完整目标音轨。
- Seedance 使用 Seed Audio 最终音频作为输入。
- 最终 mux 使用与 Seedance 输入完全相同的音频。
- 多角色视频中角色和台词能够稳定对应。
- 目标语言口型与最终音频同步。
- 支持继续使用 Uguu 参考素材。
- 不保留旧链路 fallback。
- 原项目无关功能不得受到影响。

---

## 25. 最终目标架构

```text
                        ┌───────────────────────┐
                        │      原始视频          │
                        └───────────┬───────────┘
                                    │
                                    ▼
                     ┌──────────────────────────┐
                     │ Doubao Seed 2.0 Lite     │
                     │                          │
                     │ 音频 + 画面联合理解       │
                     │ 角色 + 台词 + 时间 + 翻译 │
                     └────────────┬─────────────┘
                                  │
                                  ▼
                     ┌──────────────────────────┐
原始完整音频 ────────►│      Seed Audio 1.0      │
                     │                          │
角色目标脚本 ────────►│ 完整本地化声音场景生成    │
                     └────────────┬─────────────┘
                                  │
                         localized_audio
                                  │
                                  ▼
                     ┌──────────────────────────┐
原始视频 ────────────►│         Seedance         │
localized_audio ─────►│                          │
Uguu参考素材 ─────────►│ 人物/场景本地化 + 口型    │
                     └────────────┬─────────────┘
                                  │
                                  ▼
                     ┌──────────────────────────┐
                     │          FFmpeg           │
                     │ video + localized_audio  │
                     └────────────┬─────────────┘
                                  │
                                  ▼
                         最终多国本地化视频
```
