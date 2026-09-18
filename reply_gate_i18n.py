"""Locale resources for Reply Gate.

AstrBot reads ``.astrbot-plugin/i18n/<locale>.json`` for the plugin page (see
docs/zh/dev/star/guides/plugin-i18n.md). The same files carry a ``messages``
section, which this module serves to the chat-facing replies, so a locale is
described in exactly one place.

Nothing here imports AstrBot, so the lookup rules are unit tested on their own.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_LOCALE = "en-US"
I18N_SUBDIR = Path(".astrbot-plugin") / "i18n"

_LOCALE_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "en": "en-US",
    "en-us": "en-US",
    "en-gb": "en-US",
}


def load_i18n(plugin_dir: str | Path) -> dict[str, dict]:
    """Read every locale file, skipping ones that cannot be parsed."""
    directory = Path(plugin_dir) / I18N_SUBDIR
    resources: dict[str, dict] = {}
    if not directory.is_dir():
        return resources
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            resources[path.stem] = data
    return resources


def normalize_locale(value: Any) -> str | None:
    """Map a language tag onto a bundled locale name, or return it unchanged."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    # Some configs use ``zh_cn`` instead of ``zh-CN``; treat both alike.
    return _LOCALE_ALIASES.get(stripped.lower().replace("_", "-"), stripped)


def resolve_locale(configured: Any, bot_language: Any = None) -> str:
    """Pick the chat-side locale.

    An explicit plugin setting wins. ``auto`` follows AstrBot's own ``language``
    setting when it is set, and otherwise uses English.
    """
    explicit = normalize_locale(configured)
    if explicit is not None and explicit.lower() != "auto":
        return explicit
    return normalize_locale(bot_language) or DEFAULT_LOCALE


class Translator:
    """Look up ``messages.<key>`` with a default-locale fallback."""

    def __init__(
        self,
        locale: str | None,
        resources: Mapping[str, Any] | None,
        *,
        default_locale: str = DEFAULT_LOCALE,
    ) -> None:
        self.locale = locale or default_locale
        self._resources = resources or {}
        self._default_locale = default_locale

    def _messages(self, locale: str) -> Mapping[str, Any]:
        data = self._resources.get(locale)
        if not isinstance(data, Mapping):
            return {}
        section = data.get("messages")
        return section if isinstance(section, Mapping) else {}

    def t(self, key: str, **kwargs: Any) -> str:
        for locale in (self.locale, self._default_locale):
            template = self._messages(locale).get(key)
            if isinstance(template, str):
                return self._render(template, kwargs)
        return key

    @staticmethod
    def _render(template: str, values: Mapping[str, Any]) -> str:
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            return template
