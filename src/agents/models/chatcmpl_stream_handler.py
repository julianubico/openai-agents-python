from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage
from dataclasses import dataclass
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseRefusalDeltaEvent,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

@dataclass
class ThinkingDeltaEvent:
    type: str
    delta: str
    sequence_number: int

from ..items import TResponseStreamEvent
from .fake_id import FAKE_RESPONSES_ID


@dataclass
class StreamingState:
    started: bool = False
    text_content_index_and_output: tuple[int, ResponseOutputText] | None = None
    refusal_content_index_and_output: tuple[int, ResponseOutputRefusal] | None = None
    function_calls: dict[int, ResponseFunctionToolCall] = field(default_factory=dict)


class SequenceNumber:
    def __init__(self):
        self._sequence_number = 0

    def get_and_increment(self) -> int:
        num = self._sequence_number
        self._sequence_number += 1
        return num


class ChatCmplStreamHandler:
    @classmethod
    async def handle_stream(
        cls,
        response: Response,
        stream: AsyncStream[ChatCompletionChunk],
    ) -> AsyncIterator[TResponseStreamEvent]:
        usage: CompletionUsage | None = None
        state = StreamingState()
        sequence_number = SequenceNumber()
        async for chunk in stream:
            if not state.started:
                state.started = True
                yield ResponseCreatedEvent(
                    response=response,
                    type="response.created",
                    sequence_number=sequence_number.get_and_increment(),
                )

            # This is always set by the OpenAI API, but not by others e.g. LiteLLM
            usage = chunk.usage if hasattr(chunk, "usage") else None

            if not chunk.choices or not chunk.choices[0].delta:
                continue

            delta = chunk.choices[0].delta

            # Handle thinking content - emit as custom events
            # Prioritize reasoning_content over thinking_blocks to avoid duplicates
            thinking_content = None
            
            if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                thinking_content = delta.reasoning_content
            elif hasattr(delta, 'thinking_blocks') and delta.thinking_blocks:
                # Only use thinking_blocks if no reasoning_content
                for tb in delta.thinking_blocks:
                    if isinstance(tb, dict) and tb.get('thinking'):
                        thinking_content = tb['thinking']
                        break  # Only take the first one
            
            if thinking_content:
                yield ThinkingDeltaEvent(
                    type="thinking.delta",
                    delta=thinking_content,
                    sequence_number=sequence_number.get_and_increment(),
                )

            # Handle text
            if delta.content:
                if not state.text_content_index_and_output:
                    # Initialize a content tracker for streaming text
                    state.text_content_index_and_output = (
                        0 if not state.refusal_content_index_and_output else 1,
                        ResponseOutputText(
                            text="",
                            type="output_text",
                            annotations=[],
                        ),
                    )
                    # Start a new assistant message stream
                    assistant_item = ResponseOutputMessage(
                        id=FAKE_RESPONSES_ID,
                        content=[],
                        role="assistant",
                        type="message",
                        status="in_progress",
                    )
                    # Notify consumers of the start of a new output message + first content part
                    yield ResponseOutputItemAddedEvent(
                        item=assistant_item,
                        output_index=0,
                        type="response.output_item.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                    yield ResponseContentPartAddedEvent(
                        content_index=state.text_content_index_and_output[0],
                        item_id=FAKE_RESPONSES_ID,
                        output_index=0,
                        part=ResponseOutputText(
                            text="",
                            type="output_text",
                            annotations=[],
                        ),
                        type="response.content_part.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                # Emit the delta for this segment of content
                yield ResponseTextDeltaEvent(
                    content_index=state.text_content_index_and_output[0],
                    delta=delta.content,
                    item_id=FAKE_RESPONSES_ID,
                    output_index=0,
                    type="response.output_text.delta",
                    sequence_number=sequence_number.get_and_increment(),
                )
                # Accumulate the text into the response part
                state.text_content_index_and_output[1].text += delta.content

            # Handle refusals (model declines to answer)
            # This is always set by the OpenAI API, but not by others e.g. LiteLLM
            if hasattr(delta, "refusal") and delta.refusal:
                if not state.refusal_content_index_and_output:
                    # Initialize a content tracker for streaming refusal text
                    state.refusal_content_index_and_output = (
                        0 if not state.text_content_index_and_output else 1,
                        ResponseOutputRefusal(refusal="", type="refusal"),
                    )
                    # Start a new assistant message if one doesn't exist yet (in-progress)
                    assistant_item = ResponseOutputMessage(
                        id=FAKE_RESPONSES_ID,
                        content=[],
                        role="assistant",
                        type="message",
                        status="in_progress",
                    )
                    # Notify downstream that assistant message + first content part are starting
                    yield ResponseOutputItemAddedEvent(
                        item=assistant_item,
                        output_index=0,
                        type="response.output_item.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                    yield ResponseContentPartAddedEvent(
                        content_index=state.refusal_content_index_and_output[0],
                        item_id=FAKE_RESPONSES_ID,
                        output_index=0,
                        part=ResponseOutputText(
                            text="",
                            type="output_text",
                            annotations=[],
                        ),
                        type="response.content_part.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                # Emit the delta for this segment of refusal
                yield ResponseRefusalDeltaEvent(
                    content_index=state.refusal_content_index_and_output[0],
                    delta=delta.refusal,
                    item_id=FAKE_RESPONSES_ID,
                    output_index=0,
                    type="response.refusal.delta",
                    sequence_number=sequence_number.get_and_increment(),
                )
                # Accumulate the refusal string in the output part
                state.refusal_content_index_and_output[1].refusal += delta.refusal

            # Handle tool calls
            # Because we don't know the name of the function until the end of the stream, we'll
            # save everything and yield events at the end
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    if tc_delta.index not in state.function_calls:
                        state.function_calls[tc_delta.index] = ResponseFunctionToolCall(
                            id=FAKE_RESPONSES_ID,
                            arguments="",
                            name="",
                            type="function_call",
                            call_id="",
                        )
                    tc_function = tc_delta.function

                    state.function_calls[tc_delta.index].arguments += (
                        tc_function.arguments if tc_function else ""
                    ) or ""
                    state.function_calls[tc_delta.index].name += (
                        tc_function.name if tc_function else ""
                    ) or ""
                    state.function_calls[tc_delta.index].call_id += tc_delta.id or ""

        function_call_starting_index = 0
        if state.text_content_index_and_output:
            function_call_starting_index += 1
            # Send end event for this content part
            yield ResponseContentPartDoneEvent(
                content_index=state.text_content_index_and_output[0],
                item_id=FAKE_RESPONSES_ID,
                output_index=0,
                part=state.text_content_index_and_output[1],
                type="response.content_part.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        if state.refusal_content_index_and_output:
            function_call_starting_index += 1
            # Send end event for this content part
            yield ResponseContentPartDoneEvent(
                content_index=state.refusal_content_index_and_output[0],
                item_id=FAKE_RESPONSES_ID,
                output_index=0,
                part=state.refusal_content_index_and_output[1],
                type="response.content_part.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        # Actually send events for the function calls
        for function_call in state.function_calls.values():
            # First, a ResponseOutputItemAdded for the function call
            yield ResponseOutputItemAddedEvent(
                item=ResponseFunctionToolCall(
                    id=FAKE_RESPONSES_ID,
                    call_id=function_call.call_id,
                    arguments=function_call.arguments,
                    name=function_call.name,
                    type="function_call",
                ),
                output_index=function_call_starting_index,
                type="response.output_item.added",
                sequence_number=sequence_number.get_and_increment(),
            )
            # Then, yield the args
            yield ResponseFunctionCallArgumentsDeltaEvent(
                delta=function_call.arguments,
                item_id=FAKE_RESPONSES_ID,
                output_index=function_call_starting_index,
                type="response.function_call_arguments.delta",
                sequence_number=sequence_number.get_and_increment(),
            )
            # Finally, the ResponseOutputItemDone
            yield ResponseOutputItemDoneEvent(
                item=ResponseFunctionToolCall(
                    id=FAKE_RESPONSES_ID,
                    call_id=function_call.call_id,
                    arguments=function_call.arguments,
                    name=function_call.name,
                    type="function_call",
                ),
                output_index=function_call_starting_index,
                type="response.output_item.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        # Finally, send the Response completed event
        outputs: list[ResponseOutputItem] = []
        if state.text_content_index_and_output or state.refusal_content_index_and_output:
            assistant_msg = ResponseOutputMessage(
                id=FAKE_RESPONSES_ID,
                content=[],
                role="assistant",
                type="message",
                status="completed",
            )
            if state.text_content_index_and_output:
                assistant_msg.content.append(state.text_content_index_and_output[1])
            if state.refusal_content_index_and_output:
                assistant_msg.content.append(state.refusal_content_index_and_output[1])
            outputs.append(assistant_msg)

            # send a ResponseOutputItemDone for the assistant message
            yield ResponseOutputItemDoneEvent(
                item=assistant_msg,
                output_index=0,
                type="response.output_item.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        for function_call in state.function_calls.values():
            outputs.append(function_call)

        final_response = response.model_copy()
        final_response.output = outputs
        final_response.usage = (
            ResponseUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                output_tokens_details=OutputTokensDetails(
                    reasoning_tokens=usage.completion_tokens_details.reasoning_tokens
                    if usage.completion_tokens_details
                    and usage.completion_tokens_details.reasoning_tokens
                    else 0
                ),
                input_tokens_details=InputTokensDetails(
                    cached_tokens=usage.prompt_tokens_details.cached_tokens
                    if usage.prompt_tokens_details and usage.prompt_tokens_details.cached_tokens
                    else 0
                ),
            )
            if usage
            else None
        )

        yield ResponseCompletedEvent(
            response=final_response,
            type="response.completed",
            sequence_number=sequence_number.get_and_increment(),
        )
