from tools.llm_output import parse_json


def test_parse_json_ignores_braces_in_prose_and_strings():
    raw = 'Here is the result: {"message":"keep ] and } inside text","items":[1,2]} trailing prose'
    assert parse_json(raw, context="test") == {
        "message": "keep ] and } inside text",
        "items": [1, 2],
    }


def test_parse_json_handles_bom_and_fence():
    assert parse_json("\ufeff```json\n{\"ok\":true}\n```", context="test") == {"ok": True}
