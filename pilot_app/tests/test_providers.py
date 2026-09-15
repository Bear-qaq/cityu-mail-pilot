import unittest
from unittest import mock

from pilot_app import providers


class ProviderTests(unittest.TestCase):
    def test_catalog_includes_common_and_custom_providers(self):
        ids = {item["id"] for item in providers.public_catalog()["models"]}
        self.assertTrue({"openai", "anthropic", "gemini", "volcengine_ark", "deepseek", "qwen", "custom_openai"}.issubset(ids))
        self.assertEqual(providers.MODEL_PRESETS["together"].base_url, "https://api.together.ai/v1")

    def test_openai_responses_disables_storage(self):
        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}) as request:
            self.assertEqual(providers.generate_text(provider="openai", model="gpt-test", api_key="secret", prompt="hello"), "ok")
        payload = request.call_args.kwargs["payload"]
        self.assertIs(payload["store"], False)

    def test_gemini_keeps_key_out_of_url(self):
        response = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            providers.generate_text(provider="gemini", model="gemini-test", api_key="top-secret", prompt="hello")
        self.assertNotIn("top-secret", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["headers"]["x-goog-api-key"], "top-secret")

    def test_search_filters_unsafe_result_urls(self):
        response = {"results": [
            {"title": "good", "url": "https://example.com/a", "content": "x"},
            {"title": "bad", "url": "javascript:alert(1)", "content": "x"},
        ]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            results = providers.web_search("tavily", "secret", "CityU communication engineering")
        self.assertEqual([item["title"] for item in results], ["good"])


    # -- an empty answer is a failure, not a blank report ------------------

    def test_reasoning_model_that_burns_its_budget_is_reported_clearly(self):
        """deepseek-flash really does this: all 4000 max_tokens came back as
        reasoning_tokens, finish_reason "length", content "". Returning "" would
        be rendered as a seven-section report whose every line says "无"."""
        response = {
            "choices": [{"finish_reason": "length",
                         "message": {"role": "assistant", "content": "",
                                     "reasoning_content": "We need answer strictly format..."}}],
            "usage": {"completion_tokens": 4000,
                      "completion_tokens_details": {"reasoning_tokens": 4000}},
        }
        with mock.patch.object(providers, "_json_request", return_value=response):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.generate(provider="deepseek", model="deepseek-flash",
                                   api_key="k", prompt="p", max_output_tokens=4000)
        message = str(caught.exception)
        self.assertIn("隐藏推理", message)
        self.assertIn("deepseek-chat", message, "错误信息必须给出可执行的下一步")

    def test_a_few_stray_characters_after_exhausted_reasoning_also_fail(self):
        """Measured on the real API: one run returned 5 characters after 3995
        reasoning tokens. "Not empty" is not the same as "usable"."""
        response = {
            "choices": [{"finish_reason": "length",
                         "message": {"content": "\n\n无。", "reasoning_content": "thinking..."}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 3995}},
        }
        with mock.patch.object(providers, "_json_request", return_value=response):
            with self.assertRaises(providers.ProviderError):
                providers.generate(provider="deepseek", model="deepseek-flash",
                                   api_key="k", prompt="p", max_output_tokens=4000)

    def test_a_long_answer_that_used_reasoning_is_not_rejected(self):
        """A reasoning model that thinks and then writes a real report is fine —
        the guard is about wasted budgets, not about reasoning itself."""
        response = {
            "choices": [{"finish_reason": "stop", "message": {"content": "结论：" + "内容" * 300}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 3500}},
        }
        with mock.patch.object(providers, "_json_request", return_value=response):
            result = providers.generate(provider="deepseek", model="m", api_key="k",
                                        prompt="p", max_output_tokens=4000)
        self.assertGreater(len(result.text), 400)

    def test_plain_empty_answer_is_also_an_error(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.generate(provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertIn("空正文", str(caught.exception))

    def test_a_normal_answer_still_returns(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": " 结论：通过 "}}]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            result = providers.generate(provider="deepseek", model="deepseek-chat", api_key="k", prompt="p")
        self.assertEqual(result.text, "结论：通过")

    def test_a_missing_choices_key_does_not_crash(self):
        with mock.patch.object(providers, "_json_request", return_value={"error": "weird body"}):
            with self.assertRaises(providers.ProviderError):
                providers.generate(provider="deepseek", model="m", api_key="k", prompt="p")

    # -- native web search -------------------------------------------------

    def test_native_search_capability_is_declared_and_published(self):
        self.assertTrue(providers.supports_native_search("openai"))
        self.assertTrue(providers.supports_native_search("anthropic"))
        self.assertTrue(providers.supports_native_search("gemini"))
        # OpenAI-compatible providers have no built-in search of their own.
        for provider in ("deepseek", "qwen", "openrouter", "custom_openai", "azure_openai"):
            self.assertFalse(providers.supports_native_search(provider), provider)
        catalog = {item["id"]: item for item in providers.public_catalog()["models"]}
        self.assertTrue(catalog["openai"]["native_search"])
        self.assertFalse(catalog["deepseek"]["native_search"])
        self.assertIn("native_search", catalog["custom_openai"])

    def test_openai_native_search_sends_tool_and_reads_citations(self):
        response = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "report body", "annotations": [
                {"type": "url_citation", "title": "Source A", "url": "https://example.com/a"},
                {"type": "url_citation", "title": "dup", "url": "https://example.com/a"},
                {"type": "url_citation", "title": "bad", "url": "javascript:alert(1)"},
            ]},
        ]}]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            result = providers.generate(provider="openai", model="gpt-test", api_key="secret",
                                        prompt="hello", native_search=True)
        self.assertEqual(result.text, "report body")
        self.assertEqual(result.search_mode, "native")
        self.assertEqual([item["url"] for item in result.sources], ["https://example.com/a"])
        self.assertEqual(request.call_args.kwargs["payload"]["tools"], [{"type": "web_search"}])

    def test_anthropic_native_search_sends_server_tool(self):
        response = {"content": [
            {"type": "text", "text": "report body"},
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "title": "Source B", "url": "https://example.com/b"},
            ]},
        ]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            result = providers.generate(provider="anthropic", model="claude-test", api_key="secret",
                                        prompt="hello", native_search=True)
        self.assertEqual(result.text, "report body")
        self.assertEqual([item["url"] for item in result.sources], ["https://example.com/b"])
        self.assertEqual(
            request.call_args.kwargs["payload"]["tools"],
            [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        )

    def test_gemini_native_search_sends_grounding_tool(self):
        response = {"candidates": [{
            "content": {"parts": [{"text": "report body"}]},
            "groundingMetadata": {"groundingChunks": [{"web": {"title": "Source C", "uri": "https://example.com/c"}}]},
        }]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            result = providers.generate(provider="gemini", model="gemini-test", api_key="secret",
                                        prompt="hello", native_search=True)
        self.assertEqual(result.text, "report body")
        self.assertEqual([item["url"] for item in result.sources], ["https://example.com/c"])
        self.assertEqual(request.call_args.kwargs["payload"]["tools"], [{"google_search": {}}])

    def test_native_search_is_not_sent_unless_requested_or_supported(self):
        response = {"choices": [{"message": {"content": "ok"}}]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            providers.generate(provider="deepseek", model="deepseek-chat", api_key="secret",
                               prompt="hello", native_search=True)
        self.assertNotIn("tools", request.call_args.kwargs["payload"])

        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}) as request:
            result = providers.generate(provider="openai", model="gpt-test", api_key="secret", prompt="hello")
        self.assertNotIn("tools", request.call_args.kwargs["payload"])
        self.assertEqual(result.sources, [])
        self.assertEqual(result.search_mode, "none")

    def test_model_calls_allow_slow_report_generation(self):
        # A real Doubao run exceeded the 120s default, so the generation calls
        # must pass an explicit, longer timeout.
        self.assertGreaterEqual(providers.MODEL_TIMEOUT_SECONDS, 180)
        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}) as request:
            providers.generate(provider="openai", model="gpt-test", api_key="secret", prompt="hello")
        self.assertEqual(request.call_args.kwargs["timeout"], providers.MODEL_TIMEOUT_SECONDS)

    def test_read_timeout_becomes_an_actionable_error(self):
        import socket
        with mock.patch.object(providers.urllib.request, "urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(providers.ProviderError) as ctx:
                providers._json_request("https://example.com/x", headers={}, payload={"a": 1}, timeout=7)
        self.assertIn("超时", str(ctx.exception))

    def test_generate_text_still_returns_plain_text(self):
        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}):
            self.assertEqual(
                providers.generate_text(provider="openai", model="gpt-test", api_key="secret", prompt="hello"),
                "ok",
            )


if __name__ == "__main__":
    unittest.main()
