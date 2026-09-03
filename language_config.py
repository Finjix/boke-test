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
    TargetLocale("Chinese", "zh", "China", "zh-CN", "China (Chinese)"),
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

# H3's documented native dialogue-language set. Region and locale are still
# passed separately as cultural prompt constraints; the model does not promise
# a particular national dialect.
H3_NATIVE_LANGUAGE_CODES = frozenset(
    {
        "ar",
        "zh",
        "en",
        "fr",
        "de",
        "it",
        "ja",
        "ko",
        "pt",
        "ru",
        "es",
    }
)


def is_h3_native_language(language_code: str) -> bool:
    # H3_TARGET_LOCALES is built before the alias table below is initialized;
    # use the code path during module initialization and the canonical alias
    # path for all later callers.
    canonicalizer = globals().get("canonical_language_code")
    normalized = (
        canonicalizer(language_code)
        if callable(canonicalizer)
        else language_code.strip().casefold()
    )
    return normalized in H3_NATIVE_LANGUAGE_CODES


H3_TARGET_LOCALES: tuple[TargetLocale, ...] = tuple(
    item for item in TARGET_LOCALES if is_h3_native_language(item.language_code)
)


# Keep language-name and ISO-code comparison in one place so locale validation
# remains consistent across the GUI and API prompt builders.
LANGUAGE_CODE_ALIASES: dict[str, str] = {}
for _locale in TARGET_LOCALES:
    LANGUAGE_CODE_ALIASES[_locale.language.casefold()] = _locale.language_code.casefold()
    LANGUAGE_CODE_ALIASES[_locale.language_code.casefold()] = _locale.language_code.casefold()


def canonical_language_code(value: str) -> str:
    """Return the configured ISO code for a language name or code."""

    normalized = " ".join(value.strip().casefold().split())
    return LANGUAGE_CODE_ALIASES.get(normalized, normalized)


def language_values_match(left: str, right: str) -> bool:
    """Compare language names and ISO codes as equivalent values."""

    return canonical_language_code(left) == canonical_language_code(right)


def locale_from_label(label: str) -> TargetLocale | None:
    for locale in TARGET_LOCALES:
        if locale.label == label:
            return locale
    return None


def locale_from_code(code: str) -> TargetLocale | None:
    normalized = code.strip().casefold()
    for locale in TARGET_LOCALES:
        if locale.locale_code.casefold() == normalized:
            return locale
    return None
