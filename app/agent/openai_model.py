import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import openai
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallParam,
    ResponseInputParam,
    ResponseOutputMessage,
    ResponseReasoningItem,
    ToolParam,
)
from openai.types.shared.reasoning_effort import ReasoningEffort
from pydantic import SecretStr, ValidationError

from app.agent.contracts import (
    ModelAnswer,
    ModelMessage,
    ModelTurn,
    ToolCall,
    ToolDefinition,
)
from app.agent.errors import (
    AgentUnavailable,
    InvalidModelResponse,
    ProviderConfigurationError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.agent.prompt import SYSTEM_PROMPT
from app.clients.openai import openai_client
from app.core.errors import IntelligenceError


class OpenAIModel:
    """Official SDK adapter. Inputs contain only messages and tool schemas, never auth context."""

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        model: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        max_output_tokens: int = 3000,
        concise: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = reasoning_effort
        self._output_limit = max_output_tokens
        self._concise = concise
        # Provider protocol context only, scoped to this chat's adapter instance.
        # The Core still owns the transcript, execution and all call limits.
        self._replay: dict[tuple[ToolCall, ...], ResponseInputParam] = {}

    async def respond(
        self, messages: tuple[ModelMessage, ...], tools: tuple[ToolDefinition, ...]
    ) -> ModelTurn:
        if not any(message.calls for message in messages):
            self._replay.clear()
        wire: ResponseInputParam = []
        for message in messages:
            if message.role in ("user", "developer"):
                wire.append({"role": message.role, "content": message.content})
            elif message.role == "assistant" and not message.calls:
                wire.append({"role": "assistant", "content": message.content})
            elif message.role == "tool":
                wire.append(
                    {
                        "type": "function_call_output",
                        "output": message.content,
                        "call_id": message.call_id,
                    }
                )
            else:
                replay = self._replay.get(message.calls)
                if replay is not None:
                    wire.extend(replay)
                else:
                    wire.extend(
                        {
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        for call in message.calls
                    )
        schemas: list[ToolParam] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.arguments.model_json_schema(),
                "strict": True,
            }
            for tool in tools
        ]
        error: IntelligenceError
        try:
            response = await self._client.responses.create(
                model=self._model,
                instructions=SYSTEM_PROMPT
                + (
                    "\nFor spoken interaction, answer essentials in 2-4 sentences normally. "
                    "Keep necessary facts and clarification; do not recite presentation metadata."
                    if self._concise
                    else ""
                ),
                input=wire,
                tools=schemas,
                tool_choice="none" if not tools else openai.omit,
                parallel_tool_calls=False,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "management_answer",
                        "strict": True,
                        "schema": ModelAnswer.model_json_schema(),
                    },
                },
                max_output_tokens=self._output_limit,
                reasoning={"effort": self._effort} if self._effort is not None else openai.omit,
                store=False,
                include=["reasoning.encrypted_content"],
            )
            return self._parse(response)
        except openai.APITimeoutError:
            error = ProviderTimeout()
        except (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.BadRequestError,
            openai.NotFoundError,
        ):
            error = ProviderConfigurationError()
        except openai.RateLimitError:
            error = ProviderRateLimited()
        except openai.APIResponseValidationError:
            error = InvalidModelResponse()
        except (openai.APIConnectionError, openai.APIStatusError):
            error = ProviderUnavailable()
        except (ValidationError, ValueError, TypeError, AttributeError):
            error = InvalidModelResponse()
        self._replay.clear()
        raise error

    def _parse(self, response: Response) -> ModelTurn:
        if response.status != "completed" or response.error or response.incomplete_details:
            raise ValueError("Incomplete response")
        if not isinstance(response.output, list) or not response.output:
            raise ValueError("Missing output")
        calls: list[ToolCall] = []
        replay: ResponseInputParam = []
        answer: ModelAnswer | None = None
        for item in response.output:
            # SDK response construction is permissive; validate the supported items
            # before translating them into the provider-independent Core contract.
            if item.type == "function_call":
                call = ResponseFunctionToolCall.model_validate(
                    item.model_dump(warnings=False), strict=True
                )
                if (
                    not call.call_id
                    or not call.name
                    or not call.arguments
                    or call.status not in (None, "completed")
                    or call.namespace is not None
                    or (call.caller is not None and call.caller.type != "direct")
                ):
                    raise ValueError("Malformed function call")
                calls.append(ToolCall(call.call_id, call.name, call.arguments))
                wire_call: ResponseFunctionToolCallParam = {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                if call.id is not None:
                    wire_call["id"] = call.id
                replay.append(wire_call)
            elif item.type == "reasoning":
                reasoning = ResponseReasoningItem.model_validate(
                    item.model_dump(warnings=False), strict=True
                )
                if reasoning.status not in (None, "completed"):
                    raise ValueError("Incomplete reasoning item")
                replay.append(
                    {
                        "type": "reasoning",
                        "id": reasoning.id,
                        "summary": [
                            {"type": "summary_text", "text": summary.text}
                            for summary in reasoning.summary
                        ],
                        "encrypted_content": reasoning.encrypted_content,
                    }
                )
            elif item.type == "message":
                message = ResponseOutputMessage.model_validate(
                    item.model_dump(warnings=False), strict=True
                )
                if (
                    answer is not None
                    or message.status != "completed"
                    or len(message.content) != 1
                    or message.content[0].type != "output_text"
                ):
                    raise ValueError("Unexpected output message")
                answer = ModelAnswer.model_validate_json(message.content[0].text)
            else:
                raise ValueError("Unsupported output item")
        if calls and answer is None:
            self._replay[tuple(calls)] = replay
            return ModelTurn(calls=tuple(calls))
        if answer is None or calls:
            raise ValueError("Ambiguous or missing answer")
        # Keep this run's tool protocol items available for a final-only grounding repair.
        # A new transcript clears them at respond() entry; the adapter is request-scoped.
        return ModelTurn(answer=answer)


@asynccontextmanager
async def configured_model() -> AsyncIterator[OpenAIModel]:
    # Loaded only after management auth; startup performs no provider calls.
    key = SecretStr(os.environ.get("OPENAI_API_KEY", ""))
    model = os.environ.get("SNG_INTELLIGENCE_MODEL", "").strip()
    if not key.get_secret_value().strip() or not model:
        raise AgentUnavailable()
    async with openai_client(key, request_timeout_seconds=30) as client:
        yield OpenAIModel(client, model)
