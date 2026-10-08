"""
Unit tests for gateway/translate.py.

These run with `pytest -m unit` — no Docker, no network, no LLM.
Every test name states the invariant or behaviour it guards.
"""

import json
import pytest
import sys
import os

# translate.py lives one level up; make it importable without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from translate import (
    anthropic_response_to_openai,
    error_chunk,
    finish_chunk,
    openai_messages_to_anthropic,
    openai_request_to_anthropic,
    openai_tool_choice_to_anthropic,
    openai_tools_to_anthropic,
    text_delta_chunk,
    tool_call_delta_chunk,
    tool_call_start_chunk,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Tool definition translation
# ---------------------------------------------------------------------------


class TestOpenAIToolsToAnthropic:
    def test_function_shape_is_converted(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "brain_search",
                    "description": "Search the brain",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ]
        result = openai_tools_to_anthropic(tools)
        assert result == [
            {
                "name": "brain_search",
                "description": "Search the brain",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ]

    def test_missing_description_defaults_to_empty_string(self):
        result = openai_tools_to_anthropic([{"function": {"name": "fn"}}])
        assert result[0]["description"] == ""

    def test_missing_parameters_defaults_to_empty_object_schema(self):
        result = openai_tools_to_anthropic([{"function": {"name": "fn"}}])
        assert result[0]["input_schema"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# tool_choice translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "openai_val,expected",
    [
        (None, None),
        ("none", None),
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        (
            {"type": "function", "function": {"name": "brain_search"}},
            {"type": "tool", "name": "brain_search"},
        ),
        ("unknown_value", {"type": "auto"}),
    ],
)
def test_tool_choice_mapping(openai_val, expected):
    assert openai_tool_choice_to_anthropic(openai_val) == expected


# ---------------------------------------------------------------------------
# Message translation: system messages
# ---------------------------------------------------------------------------


class TestSystemMessages:
    def test_system_message_string_is_stripped_from_messages(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        result = openai_messages_to_anthropic(msgs)
        assert all(m["role"] != "system" for m in result)

    def test_system_message_is_promoted_to_top_level_field(self):
        body = {
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ]
        }
        result = openai_request_to_anthropic(body)
        assert result["system"] == "Be concise."
        assert all(m["role"] != "system" for m in result["messages"])

    def test_multiple_system_messages_first_one_wins(self):
        """Only the first system message is promoted (next() behaviour)."""
        body = {
            "messages": [
                {"role": "system", "content": "First."},
                {"role": "system", "content": "Second."},
                {"role": "user", "content": "Hi"},
            ]
        }
        result = openai_request_to_anthropic(body)
        assert result["system"] == "First."

    def test_no_system_message_means_no_system_field(self):
        body = {"messages": [{"role": "user", "content": "Hi"}]}
        result = openai_request_to_anthropic(body)
        assert "system" not in result


# ---------------------------------------------------------------------------
# Message translation: tool calls
# ---------------------------------------------------------------------------


class TestToolCallMessages:
    def test_assistant_tool_calls_become_tool_use_content_blocks(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "brain_search",
                            "arguments": '{"query": "refund policy"}',
                        },
                    }
                ],
            }
        ]
        result = openai_messages_to_anthropic(msgs)
        assert result[0]["role"] == "assistant"
        block = result[0]["content"][0]
        assert block["type"] == "tool_use"
        assert block["id"] == "call_1"
        assert block["name"] == "brain_search"
        assert block["input"] == {"query": "refund policy"}

    def test_assistant_tool_call_with_text_includes_text_block(self):
        msgs = [
            {
                "role": "assistant",
                "content": "Let me search.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "fn", "arguments": "{}"},
                    }
                ],
            }
        ]
        result = openai_messages_to_anthropic(msgs)
        types = [b["type"] for b in result[0]["content"]]
        assert "text" in types and "tool_use" in types

    def test_role_tool_becomes_user_tool_result(self):
        msgs = [{"role": "tool", "tool_call_id": "call_1", "content": "Found: 30 days"}]
        result = openai_messages_to_anthropic(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "tool_result"
        assert result[0]["content"][0]["tool_use_id"] == "call_1"
        assert result[0]["content"][0]["content"] == "Found: 30 days"

    def test_consecutive_tool_results_are_merged_into_one_user_message(self):
        msgs = [
            {"role": "tool", "tool_call_id": "c1", "content": "Result 1"},
            {"role": "tool", "tool_call_id": "c2", "content": "Result 2"},
        ]
        result = openai_messages_to_anthropic(msgs)
        assert len(result) == 1
        assert len(result[0]["content"]) == 2

    def test_tool_result_not_merged_when_separated_by_assistant_message(self):
        msgs = [
            {"role": "tool", "tool_call_id": "c1", "content": "R1"},
            {"role": "assistant", "content": "OK"},
            {"role": "tool", "tool_call_id": "c2", "content": "R2"},
        ]
        result = openai_messages_to_anthropic(msgs)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Anthropic → OpenAI response translation
# ---------------------------------------------------------------------------


class TestAnthropicResponseToOpenAI:
    def test_text_response_is_mapped_correctly(self):
        resp = {
            "id": "msg_abc",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        result = anthropic_response_to_openai(resp, "claude-sonnet-4-6")
        assert result["choices"][0]["message"]["content"] == "Hello"
        assert result["choices"][0]["finish_reason"] == "stop"

    def test_tool_use_response_becomes_tool_calls(self):
        resp = {
            "id": "msg_1",
            "model": "claude-sonnet-4-6",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "brain_search",
                    "input": {"query": "refund"},
                }
            ],
            "stop_reason": "tool_use",
        }
        result = anthropic_response_to_openai(resp, "claude-sonnet-4-6")
        msg = result["choices"][0]["message"]
        assert msg["content"] is None
        assert msg["tool_calls"][0]["id"] == "tu_1"
        assert msg["tool_calls"][0]["function"]["name"] == "brain_search"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {
            "query": "refund"
        }
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.parametrize(
        "stop_reason,expected",
        [
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
            ("stop_sequence", "stop"),
            ("unknown", "stop"),
        ],
    )
    def test_stop_reason_mapping(self, stop_reason, expected):
        resp = {"id": "x", "model": "m", "content": [], "stop_reason": stop_reason}
        result = anthropic_response_to_openai(resp, "m")
        assert result["choices"][0]["finish_reason"] == expected

    def test_multiple_text_blocks_are_joined_with_newline_not_space(self):
        resp = {
            "id": "x",
            "model": "m",
            "content": [
                {"type": "text", "text": "Line 1"},
                {"type": "text", "text": "Line 2"},
            ],
            "stop_reason": "end_turn",
        }
        result = anthropic_response_to_openai(resp, "m")
        assert result["choices"][0]["message"]["content"] == "Line 1\nLine 2"


# ---------------------------------------------------------------------------
# Streaming chunk builders
# ---------------------------------------------------------------------------


class TestStreamingChunks:
    def test_text_delta_chunk_is_valid_sse(self):
        raw = text_delta_chunk("hello")
        assert raw.startswith(b"data: ")
        payload = json.loads(raw[6:])
        assert payload["choices"][0]["delta"]["content"] == "hello"

    def test_tool_call_start_chunk_contains_id_and_name(self):
        raw = tool_call_start_chunk(0, "call_1", "brain_search")
        payload = json.loads(raw[6:])
        tc = payload["choices"][0]["delta"]["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["function"]["name"] == "brain_search"
        assert tc["function"]["arguments"] == ""

    def test_tool_call_delta_chunk_contains_partial_json(self):
        raw = tool_call_delta_chunk(0, '{"q')
        payload = json.loads(raw[6:])
        assert (
            payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
            == '{"q'
        )

    def test_finish_chunk_maps_stop_reason(self):
        raw = finish_chunk("tool_use")
        payload = json.loads(raw[6:])
        assert payload["choices"][0]["finish_reason"] == "tool_calls"

    def test_error_chunk_is_valid_json_sse(self):
        raw = error_chunk(502, "Bad Gateway")
        assert raw.startswith(b"data: ")
        payload = json.loads(raw[6:])
        assert payload["error"]["code"] == 502
        assert payload["error"]["message"] == "Bad Gateway"
