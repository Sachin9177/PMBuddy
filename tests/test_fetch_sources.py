"""
tests/test_fetch_sources.py
Unit tests for fetch_sources.py helper functions and API fetchers.
Uses mocks for all external HTTP calls — no live APIs hit.
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fetch_sources as fs


# ---------------------------------------------------------------------------
# Pure function tests (no mocking needed)
# ---------------------------------------------------------------------------

class TestCleanHtml(unittest.TestCase):
    def test_strips_tags(self):
        self.assertEqual(fs.clean_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_empty_string(self):
        self.assertEqual(fs.clean_html(""), "")

    def test_none_returns_empty(self):
        self.assertEqual(fs.clean_html(None), "")

    def test_no_tags_unchanged(self):
        self.assertEqual(fs.clean_html("plain text"), "plain text")


class TestTruncate(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(fs.truncate("hello", 220), "hello")

    def test_long_text_truncated(self):
        text = "a" * 300
        result = fs.truncate(text, 220)
        self.assertTrue(result.endswith("..."))
        self.assertEqual(len(result), 223)  # 220 + "..."

    def test_exact_limit_unchanged(self):
        text = "a" * 220
        self.assertEqual(fs.truncate(text, 220), text)


class TestSourceNameFromUrl(unittest.TestCase):
    def test_known_domain_returns_override(self):
        self.assertEqual(fs.source_name_from_url("https://techcrunch.com/feed/"), "TechCrunch")
        self.assertEqual(fs.source_name_from_url("https://www.theverge.com/rss/index.xml"), "The Verge")

    def test_unknown_domain_titlecased(self):
        result = fs.source_name_from_url("https://example.com/feed")
        self.assertEqual(result, "Example")


class TestFormatBlock(unittest.TestCase):
    def test_no_articles_shows_placeholder(self):
        result = fs.format_block("TestSource", [])
        self.assertIn("No articles found", result)
        self.assertIn("SOURCE: TestSource", result)

    def test_article_fields_rendered(self):
        articles = [{"title": "My Title", "url": "https://example.com", "summary": "A summary", "date": "2026-04-09"}]
        result = fs.format_block("TestSource", articles)
        self.assertIn("My Title", result)
        self.assertIn("https://example.com", result)
        self.assertIn("A summary", result)

    def test_missing_optional_fields(self):
        articles = [{"title": "Title only", "url": "https://x.com", "summary": "", "date": ""}]
        result = fs.format_block("S", articles)
        self.assertIn("Title only", result)


# ---------------------------------------------------------------------------
# API fetcher tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestFetchHackerNews(unittest.TestCase):
    @patch("fetch_sources.requests.get")
    def test_returns_articles_with_urls(self, mock_get):
        # First call: top story IDs
        ids_response = MagicMock()
        ids_response.json.return_value = [1, 2]

        # Second call: story 1 (valid)
        story1 = MagicMock()
        story1.json.return_value = {
            "type": "story",
            "title": "Test Story",
            "url": "https://example.com",
            "score": 100,
        }

        # Third call: story 2 (Ask HN — no URL, should be skipped)
        story2 = MagicMock()
        story2.json.return_value = {
            "type": "story",
            "title": "Ask HN: Something",
            "url": "",
            "score": 50,
        }

        mock_get.side_effect = [ids_response, story1, story2]

        articles = fs.fetch_hackernews(max_items=5)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "Test Story")
        self.assertEqual(articles[0]["url"], "https://example.com")

    @patch("fetch_sources.requests.get")
    def test_returns_empty_on_api_failure(self, mock_get):
        mock_get.side_effect = Exception("Network error")
        articles = fs.fetch_hackernews()
        self.assertEqual(articles, [])

    @patch("fetch_sources.requests.get")
    def test_skips_non_story_types(self, mock_get):
        ids_response = MagicMock()
        ids_response.json.return_value = [99]

        item = MagicMock()
        item.json.return_value = {"type": "comment", "title": "nope", "url": "https://x.com", "score": 10}

        mock_get.side_effect = [ids_response, item]
        articles = fs.fetch_hackernews(max_items=5)
        self.assertEqual(articles, [])


class TestFetchDevTo(unittest.TestCase):
    @patch("fetch_sources.requests.get")
    def test_returns_articles(self, mock_get):
        response = MagicMock()
        response.json.return_value = [
            {"title": "PM Tips", "url": "https://dev.to/1", "description": "Good stuff", "published_at": "2026-04-09T10:00:00Z"},
        ]
        response.raise_for_status = MagicMock()
        mock_get.return_value = response

        articles = fs.fetch_devto(tags=("product-management",), max_items=5)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "PM Tips")
        self.assertEqual(articles[0]["date"], "2026-04-09")

    @patch("fetch_sources.requests.get")
    def test_deduplicates_across_tags(self, mock_get):
        same_article = {"title": "Same", "url": "https://dev.to/same", "description": "", "published_at": ""}
        response = MagicMock()
        response.json.return_value = [same_article]
        response.raise_for_status = MagicMock()
        mock_get.return_value = response

        # Two tags, same article returned for both
        articles = fs.fetch_devto(tags=("product-management", "startup"), max_items=10)
        urls = [a["url"] for a in articles]
        self.assertEqual(len(urls), len(set(urls)))  # no duplicates

    @patch("fetch_sources.requests.get")
    def test_returns_empty_on_failure(self, mock_get):
        mock_get.side_effect = Exception("Timeout")
        articles = fs.fetch_devto(tags=("product-management",))
        self.assertEqual(articles, [])


class TestFetchProductHunt(unittest.TestCase):
    def test_returns_empty_without_api_key(self):
        self.assertEqual(fs.fetch_producthunt(None), [])
        self.assertEqual(fs.fetch_producthunt("your_key_here"), [])

    @patch("fetch_sources.requests.post")
    def test_returns_launches(self, mock_post):
        response = MagicMock()
        response.json.return_value = {
            "data": {
                "posts": {
                    "edges": [
                        {"node": {"name": "CoolApp", "tagline": "Does stuff", "url": "https://ph.co/1", "votesCount": 200, "createdAt": "2026-04-09T08:00:00Z"}},
                    ]
                }
            }
        }
        response.raise_for_status = MagicMock()
        mock_post.return_value = response

        articles = fs.fetch_producthunt("valid_key")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "CoolApp")
        self.assertIn("200 votes", articles[0]["summary"])

    @patch("fetch_sources.requests.post")
    def test_returns_empty_on_api_failure(self, mock_post):
        mock_post.side_effect = Exception("API down")
        articles = fs.fetch_producthunt("valid_key")
        self.assertEqual(articles, [])

    @patch("fetch_sources.requests.post")
    def test_handles_empty_edges(self, mock_post):
        response = MagicMock()
        response.json.return_value = {"data": {"posts": {"edges": []}}}
        response.raise_for_status = MagicMock()
        mock_post.return_value = response

        articles = fs.fetch_producthunt("valid_key")
        self.assertEqual(articles, [])


if __name__ == "__main__":
    unittest.main()
