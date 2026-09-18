"""Locale lookup rules (no AstrBot runtime required)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from choice_gate_i18n import Translator, load_i18n, normalize_locale, resolve_locale  # noqa: E402

RESOURCES = {
    "en-US": {"messages": {"only_en": "Only English", "both": "Both {value}"}},
    "zh-CN": {"messages": {"both": "都 {value}"}},
}


def test_load_i18n_reads_locale_files_and_skips_broken_ones(tmp_path):
    directory = tmp_path / ".astrbot-plugin" / "i18n"
    directory.mkdir(parents=True)
    (directory / "en-US.json").write_text(json.dumps({"metadata": {"x": 1}}), encoding="utf-8")
    (directory / "zh-CN.json").write_text("{not json", encoding="utf-8")
    (directory / "broken.json").write_text("[1, 2]", encoding="utf-8")
    assert load_i18n(tmp_path) == {"en-US": {"metadata": {"x": 1}}}
    assert load_i18n(tmp_path / "missing") == {}


def test_normalize_locale_maps_aliases_and_passes_others_through():
    assert normalize_locale("zh") == "zh-CN"
    assert normalize_locale(" ZH_cn ") == "zh-CN"
    assert normalize_locale("en") == "en-US"
    assert normalize_locale("ja-JP") == "ja-JP"
    assert normalize_locale("") is None
    assert normalize_locale(None) is None


def test_resolve_locale_prefers_the_plugin_setting_then_the_bot_language():
    assert resolve_locale("zh-CN", "en-US") == "zh-CN"
    assert resolve_locale("zh", "en-US") == "zh-CN"
    assert resolve_locale("auto", "zh") == "zh-CN"
    assert resolve_locale("auto", None) == "en-US"
    assert resolve_locale(None, None) == "en-US"
    assert resolve_locale("auto", "ja-JP") == "ja-JP"


def test_translator_falls_back_to_the_default_locale_then_the_key():
    translator = Translator("zh-CN", RESOURCES)
    assert translator.t("both", value=1) == "都 1"
    assert translator.t("only_en") == "Only English"
    assert translator.t("missing.key") == "missing.key"
    assert Translator("de-DE", RESOURCES).t("both", value=2) == "Both 2"


def test_translator_survives_missing_format_arguments():
    assert Translator("en-US", RESOURCES).t("both") == "Both {value}"
