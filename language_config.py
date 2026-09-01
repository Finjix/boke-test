"""Data-driven language/country choices for the GUI.

The catalog is only a user-interface convenience.  Actual availability is
determined by the configured account and the selected model; no language is
used as a Provider fallback.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetLocale:
    language: str
    region: str
    label: str


@dataclass(frozen=True)
class SourceLocale:
    language: str
    region: str
    label: str
    asr_language: str


# MediaKit's current ASR contract exposes explicit language hints for these
# two language families. Keep the source selector limited to supported values
# instead of sending a guessed language code to the CLI.
SOURCE_LOCALES: tuple[SourceLocale, ...] = (
    SourceLocale("English", "United States", "United States (English)", "eng-US"),
    SourceLocale("English", "United Kingdom", "United Kingdom (English)", "eng-US"),
    SourceLocale("Chinese", "China", "China (Chinese)", "cmn-Hans-CN"),
)


TARGET_LOCALES: tuple[TargetLocale, ...] = (
    TargetLocale("English", "United States", "United States (English)"),
    TargetLocale("English", "United Kingdom", "United Kingdom (English)"),
    TargetLocale("Spanish", "Spain", "Spain (Spanish)"),
    TargetLocale("Spanish", "Latin America", "Latin America (Spanish)"),
    TargetLocale("Portuguese", "Brazil", "Brazil (Portuguese)"),
    TargetLocale("French", "France", "France (French)"),
    TargetLocale("French", "Canada", "Canada (French)"),
    TargetLocale("German", "Germany", "Germany (German)"),
    TargetLocale("Italian", "Italy", "Italy (Italian)"),
    TargetLocale("Arabic", "Gulf", "Gulf (Arabic)"),
    TargetLocale("Turkish", "Turkey", "Turkey (Turkish)"),
    TargetLocale("Dutch", "Netherlands", "Netherlands (Dutch)"),
    TargetLocale("Polish", "Poland", "Poland (Polish)"),
    TargetLocale("Russian", "Russia", "Russia (Russian)"),
    TargetLocale("Ukrainian", "Ukraine", "Ukraine (Ukrainian)"),
    TargetLocale("Hebrew", "Israel", "Israel (Hebrew)"),
    TargetLocale("Hindi", "India", "India (Hindi)"),
    TargetLocale("Bengali", "Bangladesh", "Bangladesh (Bengali)"),
    TargetLocale("Japanese", "Japan", "Japan (Japanese)"),
    TargetLocale("Korean", "South Korea", "South Korea (Korean)"),
    TargetLocale("Thai", "Thailand", "Thailand (Thai)"),
    TargetLocale("Vietnamese", "Vietnam", "Vietnam (Vietnamese)"),
    TargetLocale("Indonesian", "Indonesia", "Indonesia (Indonesian)"),
    TargetLocale("Malay", "Malaysia", "Malaysia (Malay)"),
)


DEFAULT_TARGET_LOCALE_LABEL = "Gulf (Arabic)"


def locale_from_label(label: str) -> TargetLocale | None:
    for locale in TARGET_LOCALES:
        if locale.label == label:
            return locale
    return None


def source_locale_from_label(label: str) -> SourceLocale | None:
    for locale in SOURCE_LOCALES:
        if locale.label == label:
            return locale
    return None


def target_locales_for_source(source: SourceLocale | None) -> tuple[TargetLocale, ...]:
    """Return target choices excluding the exact source language/region pair."""

    if source is None:
        return TARGET_LOCALES
    return tuple(
        locale
        for locale in TARGET_LOCALES
        if (locale.language, locale.region) != (source.language, source.region)
    )
