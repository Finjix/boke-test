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


TARGET_LOCALES: tuple[TargetLocale, ...] = (
    TargetLocale("English", "United States", "English (United States)"),
    TargetLocale("English", "United Kingdom", "English (United Kingdom)"),
    TargetLocale("Spanish", "Spain", "Spanish (Spain)"),
    TargetLocale("Spanish", "Latin America", "Spanish (Latin America)"),
    TargetLocale("Portuguese", "Brazil", "Portuguese (Brazil)"),
    TargetLocale("French", "France", "French (France)"),
    TargetLocale("French", "Canada", "French (Canada)"),
    TargetLocale("German", "Germany", "German (Germany)"),
    TargetLocale("Italian", "Italy", "Italian (Italy)"),
    TargetLocale("Arabic", "Gulf", "Arabic (Gulf)"),
    TargetLocale("Turkish", "Turkey", "Turkish (Turkey)"),
    TargetLocale("Dutch", "Netherlands", "Dutch (Netherlands)"),
    TargetLocale("Polish", "Poland", "Polish (Poland)"),
    TargetLocale("Russian", "Russia", "Russian (Russia)"),
    TargetLocale("Ukrainian", "Ukraine", "Ukrainian (Ukraine)"),
    TargetLocale("Hebrew", "Israel", "Hebrew (Israel)"),
    TargetLocale("Hindi", "India", "Hindi (India)"),
    TargetLocale("Bengali", "Bangladesh", "Bengali (Bangladesh)"),
    TargetLocale("Japanese", "Japan", "Japanese (Japan)"),
    TargetLocale("Korean", "South Korea", "Korean (South Korea)"),
    TargetLocale("Thai", "Thailand", "Thai (Thailand)"),
    TargetLocale("Vietnamese", "Vietnam", "Vietnamese (Vietnam)"),
    TargetLocale("Indonesian", "Indonesia", "Indonesian (Indonesia)"),
    TargetLocale("Malay", "Malaysia", "Malay (Malaysia)"),
)


def locale_from_label(label: str) -> TargetLocale | None:
    for locale in TARGET_LOCALES:
        if locale.label == label:
            return locale
    return None
