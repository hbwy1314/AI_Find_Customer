"""Tests for the validator-text Chinese fallback normalizer."""

from agents.email_craft_agent import (
    _localize_validator_list,
    _localize_validator_text,
)


def test_email_n_rewritten_to_chinese_ordinal():
    out = _localize_validator_text("Email 1 has a grammatical error")
    # "Email 1" → "第 1 封" and "grammatical error" → "语法问题"
    assert "第 1 封" in out
    assert "Email 1" not in out
    assert "语法问题" in out
    assert "grammatical" not in out
    out2 = _localize_validator_text("Email 2 subject line repeats")
    assert "第 2 封" in out2
    assert "主题行" in out2
    assert "Email 2" not in out2


def test_common_validator_phrases_translated():
    samples = {
        "grammar error": "语法问题",
        "spelling issue": "拼写问题",
        "weak CTA": "weak 行动号召",
        "subject line": "主题行",
        "closing statement": "结尾段落",
        "too short": "过短",
        "too aggressive": "过于激进",
        "lacks proof points": "lacks 佐证要点",
        "call to action is missing": "行动号召 is missing",
        "buyer-oriented language": "面向买方的 language",
    }
    for src, expected in samples.items():
        assert _localize_validator_text(src) == expected


def test_empty_input_passthrough():
    assert _localize_validator_text("") == ""
    assert _localize_validator_text(None) is None  # type: ignore[arg-type]


def test_list_helpers():
    out = _localize_validator_list(["Email 1 has a grammar error", "Revise CTA"])
    assert "第 1 封" in out[0]
    assert "语法问题" in out[0]
    assert "行动号召" in out[1]
    assert _localize_validator_list([]) == []
    assert _localize_validator_list(None) == []


def test_pure_chinese_passthrough():
    """Chinese-only text should not be mangled by the phrase map."""
    text = "第 1 封语法未通过，建议重写"
    assert _localize_validator_text(text) == text
