"""Data-driven target-language choices for the GUI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetLocale:
    language: str
    language_code: str
    region: str
    locale_code: str
    label: str


TARGET_LOCALES: tuple[TargetLocale, ...] = (
    TargetLocale("English", "en", "United States", "en-US", "United States (English)"),
    TargetLocale("English", "en", "United Kingdom", "en-GB", "United Kingdom (English)"),
    TargetLocale("Spanish", "es", "Spain", "es-ES", "Spain (Spanish)"),
    TargetLocale("Spanish", "es", "Latin America", "es-419", "Latin America (Spanish)"),
    TargetLocale("Portuguese", "pt", "Brazil", "pt-BR", "Brazil (Portuguese)"),
    TargetLocale("French", "fr", "France", "fr-FR", "France (French)"),
    TargetLocale("French", "fr", "Canada", "fr-CA", "Canada (French)"),
    TargetLocale("German", "de", "Germany", "de-DE", "Germany (German)"),
    TargetLocale("Italian", "it", "Italy", "it-IT", "Italy (Italian)"),
    TargetLocale("Arabic", "ar", "Gulf", "ar-SA", "Gulf (Arabic)"),
    TargetLocale("Turkish", "tr", "Turkey", "tr-TR", "Turkey (Turkish)"),
    TargetLocale("Dutch", "nl", "Netherlands", "nl-NL", "Netherlands (Dutch)"),
    TargetLocale("Polish", "pl", "Poland", "pl-PL", "Poland (Polish)"),
    TargetLocale("Russian", "ru", "Russia", "ru-RU", "Russia (Russian)"),
    TargetLocale("Ukrainian", "uk", "Ukraine", "uk-UA", "Ukraine (Ukrainian)"),
    TargetLocale("Hebrew", "he", "Israel", "he-IL", "Israel (Hebrew)"),
    TargetLocale("Hindi", "hi", "India", "hi-IN", "India (Hindi)"),
    TargetLocale("Bengali", "bn", "Bangladesh", "bn-BD", "Bangladesh (Bengali)"),
    TargetLocale("Japanese", "ja", "Japan", "ja-JP", "Japan (Japanese)"),
    TargetLocale("Korean", "ko", "South Korea", "ko-KR", "South Korea (Korean)"),
    TargetLocale("Thai", "th", "Thailand", "th-TH", "Thailand (Thai)"),
    TargetLocale("Vietnamese", "vi", "Vietnam", "vi-VN", "Vietnam (Vietnamese)"),
    TargetLocale("Indonesian", "id", "Indonesia", "id-ID", "Indonesia (Indonesian)"),
    TargetLocale("Malay", "ms", "Malaysia", "ms-MY", "Malaysia (Malay)"),
)


DEFAULT_TARGET_LOCALE_LABEL = "Gulf (Arabic)"


def locale_from_label(label: str) -> TargetLocale | None:
    for locale in TARGET_LOCALES:
        if locale.label == label:
            return locale
    return None
