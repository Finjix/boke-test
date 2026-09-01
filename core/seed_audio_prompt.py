"""Prompt construction for Seed-Audio 1.0 dry dialogue generation."""

from __future__ import annotations

from collections.abc import Sequence

from core.models import Segment, SpeakerProfile
from utils.errors import ValidationError


_GENDER = {"male": "男性", "female": "女性", "unknown": "性别未知"}
_AGE = {
    "child": "儿童",
    "young": "年轻",
    "middle": "中年",
    "elderly": "老年",
    "unknown": "年龄未知",
}
_ROLE = {
    "main_character": "主角",
    "supporting": "配角",
    "narrator": "旁白",
    "unknown": "角色未知",
}


def _profile_description(profile: SpeakerProfile) -> str:
    styles = "、".join(profile.voice_style) or "中性"
    return (
        f"{_AGE[profile.age_group]}{_GENDER[profile.gender]}；"
        f"{styles}；{_ROLE[profile.role_type]}"
    )


def build_seed_audio_prompt(
    translated: Sequence[Segment],
    profiles: Sequence[SpeakerProfile],
    *,
    duration: float,
    target_language: str,
    target_region: str,
) -> str:
    if duration <= 0:
        raise ValidationError("video duration must be positive for Seed-Audio prompt")
    if not translated:
        raise ValidationError("translated timeline cannot be empty")

    profile_map = {profile.speaker_id: profile for profile in profiles}
    missing = sorted({segment.speaker for segment in translated} - set(profile_map))
    if missing:
        raise ValidationError(f"missing speaker profiles: {', '.join(missing)}")

    lines = [
        "任务：为视频生成多角色干声对白轨。",
        f"目标语言：{target_language}（{target_region}）。",
        "DRY DIALOGUE ONLY",
        "严格要求：",
        "1. 只生成角色对白人声。",
        "2. 不生成背景音乐。",
        "3. 不生成环境音。",
        "4. 不生成任何音效或 Foley。",
        "5. 不添加原文不存在的台词。",
        "6. 不删减已有台词。",
        "7. 每个角色声音必须明显不同。",
        "8. 根据角色性别、年龄、角色气质分配不同声音。",
        "9. 保持角色在整段音频中的声音一致。",
        "10. 严格按照给定时间段进入和结束对白。",
        f"11. 总长度严格接近 {duration:.2f} 秒。",
        "12. 没有对白的区间保持静音。",
        "13. 输出为干净、近讲、无混响的对白录音。",
        "",
        "角色定义：",
    ]
    for profile in profiles:
        lines.append(f"- {profile.speaker_id}：{_profile_description(profile)}。")
    lines.extend(("", "时间线："))
    for segment in translated:
        safe_text = segment.text.replace("\"", "\\\"").replace("\n", " ")
        lines.append(
            f'[{segment.start:05.2f} - {segment.end:05.2f}] '
            f'{segment.speaker}: "{safe_text}"'
        )
    lines.extend(("", "请严格按时间轴生成，仅输出对应对白音频。"))
    return "\n".join(lines)
