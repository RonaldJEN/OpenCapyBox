"""Tool parameters must be complete JSON objects and preserve payload contents."""

import json

import pytest

from src.agent.llm.json_parser import (
    ToolArgumentsError,
    parse_tool_arguments,
    validate_tool_arguments,
)


def test_file_content_is_preserved_exactly():
    payload = {
        "path": r"C:\Users\test\报告.md",
        "content": '测试“甲”“乙”。末尾校验\n‘甲’「乙」『丙』\n"quotes"\\尾部\t  ',
        "metadata": {"data": [None, True, 1, -1.5e-10, {"pattern": "[a-z]{2,4}"}]},
    }
    raw = json.dumps(payload, ensure_ascii=False)
    assert parse_tool_arguments(raw) == payload


@pytest.mark.parametrize("raw", ["", " \n\t ", "{}"])
def test_empty_arguments(raw):
    assert parse_tool_arguments(raw) == {}


@pytest.mark.parametrize("raw", [
    '{"content":"未完成正文',
    '{"content":"完整字符串但对象未闭合"',
    '{"path":"a.md","content":',
    '{"content":"ok","items":[1,2',
    '{"content":"ok","nested":{"tail":"missing"',
    '{"command":"python "script.py" arg"}',
    '{“content”:“错误结构引号”}',
    '{"content":"ok", broken}',
    '{"content":"ok"}{}',
    '{"content":"ok"}}',
    '{}{}',
    '{',
    'prefix {"content":"ok"}',
    '{"content":"ok"} suffix',
    '```json\n{"content":"ok"}\n```',
    '```\n{"content":"ok"}\n```',
    '```json\n{"content":"ok"}',
    '```json\n{"content":"未完成\n```',
])
def test_malformed_or_partial_arguments_are_never_salvaged(raw):
    with pytest.raises(ToolArgumentsError, match="invalid_json"):
        parse_tool_arguments(raw)


def test_error_tells_model_the_tool_was_not_executed():
    with pytest.raises(ToolArgumentsError) as error:
        parse_tool_arguments('{"content":"未完成正文')
    message = str(error.value)
    assert message.startswith("tool_arguments_invalid: invalid_json.")
    assert "not executed" in message
    assert "line" not in message and "column" not in message


@pytest.mark.parametrize("raw", [
    '{"content":"first","content":"second"}',
    '{"nested":{"path":"a","path":"b"}}',
    r'{"content":"first","\u0063ontent":"second"}',
])
def test_duplicate_keys_are_rejected_at_any_depth(raw):
    with pytest.raises(ToolArgumentsError, match="duplicate_key"):
        parse_tool_arguments(raw)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(ToolArgumentsError, match="non_finite_number"):
        parse_tool_arguments('{"nested": [' + value + ']}')


@pytest.mark.parametrize("raw", ["[]", "null", '"text"', "42", "true"])
def test_top_level_must_be_an_object(raw):
    with pytest.raises(ToolArgumentsError, match="expected_object"):
        parse_tool_arguments(raw)


@pytest.mark.parametrize("raw", [None, {}, [], 42])
def test_non_string_input_is_not_coerced(raw):
    with pytest.raises(ToolArgumentsError, match="expected_json_string"):
        parse_tool_arguments(raw)


@pytest.mark.parametrize("raw", [
    '{"private-key":"secret-body',
    '{"private-key":"a","private-key":"secret-body"}',
])
def test_error_does_not_echo_parameter_keys_or_body(raw):
    with pytest.raises(ToolArgumentsError) as error:
        parse_tool_arguments(raw)
    assert "private-key" not in str(error.value)
    assert "secret-body" not in str(error.value)


def test_sdk_decoded_input_preserves_contents_and_rejects_non_json_values():
    value = {"content": "“甲”“乙”‘丙’", "nested": [False, None, 12]}
    assert validate_tool_arguments(value) is value
    for invalid in ([], {"nested": [float("nan")]}, {1: "value"}, {"value": object()}):
        with pytest.raises(ToolArgumentsError):
            validate_tool_arguments(invalid)
