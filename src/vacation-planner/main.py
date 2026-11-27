import os
from collections.abc import AsyncIterable, Mapping, Sequence
from typing import Any

from agent_framework import Agent, ChatResponse, ChatResponseUpdate, Message
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity.aio import DefaultAzureCredential

from telemetry import VacationTelemetry


ALLOWED_OPTIONS = frozenset({
    "instructions", "max_completion_tokens", "max_tokens", "temperature", "top_p",
    "stop", "tools", "tool_choice", "parallel_tool_calls", "response_format",
    "frequency_penalty", "presence_penalty", "seed",
})
INSTRUCTIONS = """You are a practical vacation-planning assistant.
Use the destination, dates, trip length, budget, interests, accessibility needs
and travel party supplied by the user, retaining them across conversation turns.
Ask one concise clarifying question if essential details are missing.
Otherwise provide a compact day-by-day itinerary, a rough budget breakdown,
transport tips, and one weather or accessibility caveat. Mark all costs as
estimates, not live quotes. You have no booking or live-search tools; never
claim availability, reservations, or current prices. Keep responses under
150 words unless the user requests more detail. Prefer one short line per day,
one line for the budget, and one line for the caveat rather than long tables.
"""


class InstrumentedChatClient(OpenAIChatCompletionClient):
    def __init__(self, telemetry, **kwargs):
        super().__init__(**kwargs)
        self.telemetry = telemetry

    def _inner_get_response(
        self, *, messages: Sequence[Message], options: Mapping[str, Any],
        stream: bool = False, **kwargs: Any,
    ):
        response = super()._inner_get_response(
            messages=messages, options=options, stream=stream, **kwargs
        )
        request_text = (
            next((message.text for message in reversed(messages) if message.role == "user"), "")
            if self.telemetry.capture_messages else None
        )
        if stream:
            async def instrumented_stream() -> AsyncIterable[ChatResponseUpdate]:
                with self.telemetry.request(request_text) as record:
                    captured_updates: list[ChatResponseUpdate] = []
                    try:
                        async for update in response:
                            for content in update.contents:
                                if content.type == "usage":
                                    record.usage.update(content.usage_details)
                            if self.telemetry.capture_messages:
                                captured_updates.append(update)
                            yield update
                    finally:
                        if captured_updates:
                            captured = ChatResponse.from_updates(captured_updates)
                            record.response_text = captured.text
                            record.model_response_id = captured.response_id
            return self._build_response_stream(
                instrumented_stream(), response_format=options.get("response_format")
            )

        async def instrumented_response():
            with self.telemetry.request(request_text) as record:
                result = await response
                if result.usage_details:
                    record.usage.update(result.usage_details)
                if self.telemetry.capture_messages:
                    record.response_text = result.text
                    record.model_response_id = result.response_id
                return result
        return instrumented_response()


def prepare_options(_request, options):
    filtered = {key: value for key, value in options.items() if key in ALLOWED_OPTIONS}
    filtered.setdefault("max_completion_tokens", 1800)
    return filtered


def main():
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    telemetry = VacationTelemetry(model)
    credential = DefaultAzureCredential()

    async def token_provider():
        return (await credential.get_token("https://ai.azure.com/.default")).token

    client = InstrumentedChatClient(
        telemetry, model=model,
        base_url=f"{endpoint.rstrip('/')}/openai/v1/", api_key=token_provider,
    )
    agent = Agent(
        client=client, name="vacation-planner", instructions=INSTRUCTIONS,
        default_options={"store": False, "max_completion_tokens": 1800},
    )
    ResponsesHostServer(agent, prepare_options=prepare_options).run()


if __name__ == "__main__":
    main()
