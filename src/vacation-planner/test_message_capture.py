import logging
import unittest
from unittest.mock import Mock, patch

from agent_framework import ChatResponse, ChatResponseUpdate, Content, Message
from opentelemetry.sdk.trace import TracerProvider

from main import InstrumentedChatClient
from telemetry import VacationTelemetry


class RecordHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class MessageCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.telemetry = VacationTelemetry.__new__(VacationTelemetry)
        self.telemetry.model = "demo-model"
        self.telemetry.capture_messages = True
        self.telemetry.active = Mock()
        self.telemetry.requests = Mock()
        self.telemetry.duration = Mock()
        self.telemetry.tokens = Mock()
        self.provider = TracerProvider()
        self.telemetry.tracer = self.provider.get_tracer("test")
        self.handler = RecordHandler()
        self.telemetry.logger = logging.Logger("test.message.capture")
        self.telemetry.logger.addHandler(self.handler)
        self.client = InstrumentedChatClient(
            self.telemetry, model="demo-model", api_key="unused-test-key"
        )
        self.messages = [
            Message("system", ["Do not capture these instructions."]),
            Message("user", ["Plan Kyoto."]),
            Message("assistant", ["Earlier response."]),
            Message("user", ["Make day two rainy-day friendly."]),
        ]

    def tearDown(self):
        self.provider.shutdown()

    async def test_streaming_capture_reconstructs_reply_without_changing_output(self):
        updates = [
            ChatResponseUpdate(contents=[Content.from_text("Old draft")], role="assistant"),
            ChatResponseUpdate(
                contents=[Content.from_text("Visit a ")], role="assistant", merge_mode="replace"
            ),
            ChatResponseUpdate(
                contents=[Content.from_text("museum.")], role="assistant", response_id="model-123"
            ),
        ]

        async def source():
            for update in updates:
                yield update

        with patch("main.OpenAIChatCompletionClient._inner_get_response", return_value=source()):
            stream = self.client._inner_get_response(messages=self.messages, options={}, stream=True)
            returned = [update async for update in stream]
        self.assertEqual(returned, updates)
        log = self.handler.records[-1]
        self.assertEqual(log.request_text, "Make day two rainy-day friendly.")
        self.assertEqual(log.response_text, "Visit a museum.")
        self.assertEqual(log.model_response_id, "model-123")
        self.assertTrue(log.response_complete)
        self.assertNotIn("instructions", log.request_text)

    async def test_nonstreaming_capture_preserves_response_and_token_metrics(self):
        expected = ChatResponse(
            messages=[Message("assistant", [
                Content.from_text_reasoning(text="Private model reasoning."),
                Content.from_text("Visit a museum."),
            ])],
            response_id="model-456",
            usage_details={"input_token_count": 20, "output_token_count": 5},
        )

        async def source():
            return expected

        with patch("main.OpenAIChatCompletionClient._inner_get_response", return_value=source()):
            result = await self.client._inner_get_response(messages=self.messages, options={})
        self.assertIs(result, expected)
        log = self.handler.records[-1]
        self.assertEqual(log.request_text, "Make day two rainy-day friendly.")
        self.assertEqual(log.response_text, "Visit a museum.")
        self.assertEqual(log.input_token_count, 20)
        self.assertEqual(log.output_token_count, 5)

    async def test_disabled_capture_does_not_emit_message_fields(self):
        self.telemetry.capture_messages = False

        async def source():
            return ChatResponse(messages=[Message("assistant", ["Visit a museum."])])

        with patch("main.OpenAIChatCompletionClient._inner_get_response", return_value=source()):
            result = await self.client._inner_get_response(messages=self.messages, options={})
        self.assertEqual(result.text, "Visit a museum.")
        log = self.handler.records[-1]
        self.assertFalse(hasattr(log, "request_text"))
        self.assertFalse(hasattr(log, "response_text"))
        self.assertFalse(hasattr(log, "response_complete"))

    async def test_failed_stream_captures_partial_reply_and_reraises(self):
        async def source():
            yield ChatResponseUpdate(contents=[Content.from_text("Partial reply")], role="assistant")
            raise RuntimeError("Do not log this exception message.")

        with patch("main.OpenAIChatCompletionClient._inner_get_response", return_value=source()):
            stream = self.client._inner_get_response(messages=self.messages, options={}, stream=True)
            with self.assertRaises(RuntimeError):
                async for _ in stream:
                    pass
        log = self.handler.records[-1]
        self.assertEqual(log.response_text, "Partial reply")
        self.assertEqual(log.outcome, "error")
        self.assertFalse(log.response_complete)
        self.assertNotIn("exception message", log.getMessage())


if __name__ == "__main__":
    unittest.main()
