from types import SimpleNamespace

from src.agent.llm.openai_responses_client import OpenAIResponsesClient
from src.agent.schema import FunctionCall, Message, ToolCall
from src.agent.tools.apply_patch_tool import SandboxApplyPatchTool


def make_client() -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        api_key="test",
        api_base="http://127.0.0.1:1/v1",
        model="test-model",
        enable_reasoning_split=False,
    )


def test_responses_projects_apply_patch_as_freeform_custom_tool():
    tool = SandboxApplyPatchTool(SimpleNamespace())
    client = make_client()

    schema = client._convert_tools([tool])
    client._request_params([], [tool], stream=False)

    assert schema[0]["type"] == "custom"
    assert schema[0]["name"] == "apply_patch"
    assert schema[0]["format"]["syntax"] == "lark"
    assert client.last_request_snapshot["openai_protocol"] == "responses"


def test_responses_parses_custom_patch_without_json_arguments():
    item = SimpleNamespace(
        type="custom_tool_call",
        call_id="call-1",
        name="apply_patch",
        input="*** Begin Patch\n*** Delete File: old.txt\n*** End Patch",
    )
    response = SimpleNamespace(status="completed", output=[item], usage=None)

    parsed = make_client()._parse_response(response)

    assert parsed.tool_calls == [ToolCall(
        id="call-1",
        type="custom",
        function=FunctionCall(
            name="apply_patch",
            arguments={"patch": item.input},
        ),
    )]


def test_responses_replays_custom_call_and_matching_output_items():
    call = ToolCall(
        id="call-1",
        type="custom",
        function=FunctionCall(
            name="apply_patch",
            arguments={"patch": "*** Begin Patch\n*** End Patch"},
        ),
    )
    messages = [
        Message(role="assistant", content="", tool_calls=[call]),
        Message(
            role="tool",
            content="Success",
            tool_call_id="call-1",
            name="apply_patch",
        ),
    ]

    _, items = make_client()._convert_messages(messages)

    assert [item["type"] for item in items] == [
        "custom_tool_call",
        "custom_tool_call_output",
    ]
    assert items[0]["call_id"] == items[1]["call_id"] == "call-1"
