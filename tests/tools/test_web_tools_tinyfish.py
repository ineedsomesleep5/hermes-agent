"""Tests for TinyFish web backend integration."""

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest


class TestTinyFishHelpers:
    def test_headers_require_api_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TINYFISH_API_KEY", None)
            from tools.web_tools import _tinyfish_headers
            with pytest.raises(ValueError, match="TINYFISH_API_KEY"):
                _tinyfish_headers()

    def test_normalize_search_results(self):
        from tools.web_tools import _normalize_tinyfish_search_results

        result = _normalize_tinyfish_search_results({
            "results": [
                {"title": "OpenAI", "url": "https://openai.com", "snippet": "AI research", "position": 4}
            ]
        })
        assert result["success"] is True
        assert result["data"]["web"][0]["title"] == "OpenAI"
        assert result["data"]["web"][0]["description"] == "AI research"
        assert result["data"]["web"][0]["position"] == 4

    def test_normalize_documents(self):
        from tools.web_tools import _normalize_tinyfish_documents

        docs = _normalize_tinyfish_documents({
            "results": [
                {
                    "url": "https://example.com",
                    "final_url": "https://example.com/",
                    "title": "Example",
                    "text": "Example text",
                    "description": "Desc",
                    "language": "en",
                }
            ],
            "errors": [{"url": "https://bad.com", "error": "timeout"}],
        })
        assert docs[0]["url"] == "https://example.com/"
        assert docs[0]["content"] == "Example text"
        assert docs[0]["metadata"]["description"] == "Desc"
        assert docs[1]["url"] == "https://bad.com"
        assert docs[1]["error"] == "timeout"


class TestTinyFishDispatch:
    def test_search_dispatches_to_tinyfish(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [{"title": "Result", "url": "https://r.com", "snippet": "desc", "position": 1}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("tools.web_tools._get_search_backend", return_value="tinyfish"), \
             patch.dict(os.environ, {"TINYFISH_API_KEY": "tiny-test"}), \
             patch("tools.web_tools.httpx.get", return_value=mock_response) as mock_get, \
             patch("tools.interrupt.is_interrupted", return_value=False):
            from tools.web_tools import web_search_tool
            result = json.loads(web_search_tool("test query", limit=3))
            assert result["success"] is True
            assert result["data"]["web"][0]["title"] == "Result"
            assert mock_get.call_args.kwargs["params"]["query"] == "test query"

    def test_extract_dispatches_to_tinyfish(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [{"url": "https://example.com", "final_url": "https://example.com/", "text": "Extracted content", "title": "Page"}],
            "errors": [],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("tools.web_tools._get_extract_backend", return_value="tinyfish"), \
             patch.dict(os.environ, {"TINYFISH_API_KEY": "tiny-test"}), \
             patch("tools.web_tools.httpx.post", return_value=mock_response) as mock_post:
            from tools.web_tools import web_extract_tool
            result = json.loads(asyncio.get_event_loop().run_until_complete(
                web_extract_tool(["https://example.com"], use_llm_processing=False)
            ))
            assert result["results"][0]["url"] == "https://example.com/"
            assert mock_post.call_args.kwargs["json"]["urls"] == ["https://example.com"]

    def test_crawl_returns_helpful_error(self):
        with patch("tools.web_tools._get_backend", return_value="tinyfish"):
            from tools.web_tools import web_crawl_tool
            result = json.loads(asyncio.get_event_loop().run_until_complete(
                web_crawl_tool("https://example.com", use_llm_processing=False)
            ))
            assert result["success"] is False
            assert "not site crawl" in result["error"]
