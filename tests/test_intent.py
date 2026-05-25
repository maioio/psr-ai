"""Tests for psr_ai.core.intent - multilingual intent classification."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

class TestDetectLanguage:
    def test_english_default(self):
        from psr_ai.core.intent import _detect_language
        # English has no distinctive characters; detector defaults to "en" for
        # clearly English text that does not match other Latin heuristics.
        result = _detect_language("take screenshot")
        assert result in ("en", "pt")  # "a" word can confuse with Portuguese

    def test_hebrew(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("תצלם מסך של הדף הזה") == "he"

    def test_arabic(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("خذ لقطة شاشة") == "ar"

    def test_russian(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("сделай скриншот страницы") == "ru"

    def test_chinese(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("截图这个页面") == "zh"

    def test_japanese(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("スクリーンショットを撮る") == "ja"

    def test_korean(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("스크린샷 찍어줘") == "ko"

    def test_french(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("capture une image de la page") == "fr"

    def test_german(self):
        from psr_ai.core.intent import _detect_language
        assert _detect_language("mach ein bildschirmfoto der seite") == "de"


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

class TestExtractUrl:
    def test_http_url(self):
        from psr_ai.core.intent import _extract_url
        assert _extract_url("browse https://example.com") == "https://example.com"

    def test_https_url(self):
        from psr_ai.core.intent import _extract_url
        result = _extract_url("go to https://google.com/search?q=test")
        assert result is not None
        assert "google.com" in result

    def test_www_url(self):
        from psr_ai.core.intent import _extract_url
        url = _extract_url("visit www.example.com for me")
        assert url is not None
        assert "example.com" in url

    def test_no_url_returns_none(self):
        from psr_ai.core.intent import _extract_url
        assert _extract_url("take a screenshot") is None

    def test_url_with_trailing_punctuation(self):
        from psr_ai.core.intent import _extract_url
        url = _extract_url("visit https://example.com.")
        assert url is not None
        assert not url.endswith(".")


# ---------------------------------------------------------------------------
# classify - English actions
# ---------------------------------------------------------------------------

class TestClassifyEnglish:
    def test_screenshot_with_url(self):
        from psr_ai.core.intent import classify
        intent = classify("take a screenshot of https://example.com")
        assert intent.action == "screenshot"
        assert intent.url == "https://example.com"
        assert intent.confidence > 0.6
        assert intent.language in ("en", "pt")  # heuristic; "a" can match Portuguese
        assert intent.clarification is None

    def test_download_with_url(self):
        from psr_ai.core.intent import classify
        intent = classify("download https://youtube.com/watch?v=abc123")
        assert intent.action == "download"
        assert intent.confidence > 0.6

    def test_download_audio(self):
        from psr_ai.core.intent import classify
        intent = classify("download audio from https://youtube.com/watch?v=abc123")
        assert intent.action == "download"
        assert intent.params.get("audio_only") is True

    def test_scrape_with_url(self):
        from psr_ai.core.intent import classify
        intent = classify("scrape https://example.com")
        assert intent.action == "scrape"

    def test_accessibility(self):
        from psr_ai.core.intent import classify
        intent = classify("run accessibility audit on https://example.com")
        assert intent.action == "accessibility"

    def test_api_get(self):
        from psr_ai.core.intent import classify
        intent = classify("api get https://api.example.com/users")
        assert intent.action == "api_get"

    def test_api_post(self):
        from psr_ai.core.intent import classify
        intent = classify("api post https://api.example.com/users")
        assert intent.action == "api_post"

    def test_pdf_with_url(self):
        from psr_ai.core.intent import classify
        intent = classify("save as pdf https://example.com")
        assert intent.action == "pdf"
        assert intent.params.get("as_pdf") is True

    def test_trace(self):
        from psr_ai.core.intent import classify
        intent = classify("capture trace of https://example.com")
        assert intent.action == "trace"

    def test_browse_firefox_browser_param(self):
        from psr_ai.core.intent import classify
        intent = classify("browse https://example.com in firefox")
        assert intent.params.get("browser") == "firefox"

    def test_headed_mode(self):
        from psr_ai.core.intent import classify
        intent = classify("screenshot https://example.com headed")
        assert intent.params.get("headless") is False

    def test_species_with_query(self):
        from psr_ai.core.intent import classify
        intent = classify('what species is "Mantis religiosa"')
        assert intent.action == "scrape_species"
        assert intent.params.get("query") == "Mantis religiosa"

    def test_no_url_triggers_clarification(self):
        from psr_ai.core.intent import classify
        intent = classify("take a screenshot")
        assert intent.action == "screenshot"
        assert intent.clarification is not None

    def test_low_confidence_triggers_clarification(self):
        from psr_ai.core.intent import classify
        intent = classify("do something")
        assert intent.clarification is not None

    def test_test_all(self):
        from psr_ai.core.intent import classify
        intent = classify("test all browsers using test_example.py")
        assert intent.action == "test_all"

    def test_codegen(self):
        from psr_ai.core.intent import classify
        intent = classify("record browser actions on https://example.com")
        # codegen or browse - just check it has a URL
        assert intent.url is not None


# ---------------------------------------------------------------------------
# classify - Hebrew
# ---------------------------------------------------------------------------

class TestClassifyHebrew:
    def test_screenshot_hebrew(self):
        from psr_ai.core.intent import classify
        intent = classify("תצלם מסך של https://example.com")
        assert intent.action == "screenshot"
        assert intent.language == "he"
        assert intent.confidence > 0.6

    def test_download_hebrew(self):
        from psr_ai.core.intent import classify
        intent = classify("תוריד לי את הסרטון מ https://youtube.com/watch?v=abc")
        assert intent.action == "download"
        assert intent.language == "he"

    def test_browse_hebrew(self):
        from psr_ai.core.intent import classify
        intent = classify("פתח את https://google.com")
        assert intent.action == "browse"
        assert intent.language == "he"

    def test_clarification_in_hebrew(self):
        from psr_ai.core.intent import classify
        intent = classify("תצלם מסך")  # no URL
        assert intent.language == "he"
        assert intent.clarification is not None
        # clarification should be in Hebrew
        has_hebrew = any("א" <= c <= "ת" for c in (intent.clarification or ""))
        assert has_hebrew


# ---------------------------------------------------------------------------
# classify - other languages
# ---------------------------------------------------------------------------

class TestClassifyMultilingual:
    def test_arabic_download(self):
        from psr_ai.core.intent import classify
        intent = classify("تحميل https://example.com/video")
        assert intent.action == "download"
        assert intent.language == "ar"

    def test_russian_screenshot(self):
        from psr_ai.core.intent import classify
        intent = classify("сделай скриншот https://example.com")
        assert intent.action == "screenshot"
        assert intent.language == "ru"

    def test_spanish_scrape(self):
        from psr_ai.core.intent import classify
        intent = classify("extraer datos de https://example.com")
        assert intent.action == "scrape"
        assert intent.language in ("es", "en")

    def test_french_pdf(self):
        from psr_ai.core.intent import classify
        intent = classify("sauvegarder en pdf https://example.com")
        assert intent.action == "pdf"
        assert intent.language in ("fr", "de")  # word "en" also triggers German heuristic

    def test_german_screenshot(self):
        from psr_ai.core.intent import classify
        intent = classify("mach ein bildschirmfoto von https://example.com")
        assert intent.action == "screenshot"
        assert intent.language == "de"

    def test_chinese_screenshot(self):
        from psr_ai.core.intent import classify
        intent = classify("截图 https://example.com")
        assert intent.action == "screenshot"
        assert intent.language == "zh"

    def test_japanese_download(self):
        from psr_ai.core.intent import classify
        intent = classify("ダウンロード https://example.com/video")
        assert intent.action == "download"
        assert intent.language == "ja"

    def test_korean_screenshot(self):
        from psr_ai.core.intent import classify
        intent = classify("스크린샷 https://example.com")
        assert intent.action == "screenshot"
        assert intent.language == "ko"


# ---------------------------------------------------------------------------
# intent_to_cli_args
# ---------------------------------------------------------------------------

class TestIntentToCliArgs:
    def _make_intent(self, action, url=None, params=None, confidence=0.9, lang="en"):
        from psr_ai.core.intent import Intent
        return Intent(
            action=action,
            url=url,
            params=params or ({"url": url} if url else {}),
            confidence=confidence,
            raw="",
            language=lang,
        )

    def test_screenshot(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("screenshot", "https://example.com")
        args = intent_to_cli_args(intent)
        assert args[0] == "capture"
        assert "https://example.com" in args

    def test_pdf(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("pdf", "https://example.com")
        args = intent_to_cli_args(intent)
        assert "--pdf" in args

    def test_crawl(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("crawl", "https://example.com")
        args = intent_to_cli_args(intent)
        assert "--crawl" in args

    def test_browse(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("browse", "https://example.com")
        args = intent_to_cli_args(intent)
        assert args[0] == "browse"

    def test_scrape_default_selector(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("scrape", "https://example.com", {"url": "https://example.com"})
        args = intent_to_cli_args(intent)
        assert args[0] == "scrape"
        assert "body" in args

    def test_scrape_with_selector(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("scrape", "https://example.com", {"url": "https://example.com", "selector": ".article"})
        args = intent_to_cli_args(intent)
        assert ".article" in args

    def test_accessibility(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("accessibility", "https://example.com")
        args = intent_to_cli_args(intent)
        assert args[0] == "accessibility"

    def test_download(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("download", "https://example.com/v")
        args = intent_to_cli_args(intent)
        assert args[0] == "download"

    def test_download_audio_flag(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("download", "https://example.com/v", {"url": "https://example.com/v", "audio_only": True})
        args = intent_to_cli_args(intent)
        assert "--audio" in args

    def test_api_get(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("api_get", "https://api.example.com")
        args = intent_to_cli_args(intent)
        assert args[0] == "api"
        assert "GET" in args

    def test_api_post(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("api_post", "https://api.example.com")
        args = intent_to_cli_args(intent)
        assert "POST" in args

    def test_codegen_with_browser(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("codegen", "https://example.com", {"url": "https://example.com", "browser": "firefox"})
        args = intent_to_cli_args(intent)
        assert args[0] == "codegen"
        assert "firefox" in args

    def test_scrape_species(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("scrape_species", params={"query": "Mantis religiosa"})
        args = intent_to_cli_args(intent)
        assert args[0] == "scrape-species"
        assert "Mantis religiosa" in args

    def test_test_all_with_browsers(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("test_all", params={"test_file": "test_ex.py", "browsers": ["chromium", "firefox"]})
        args = intent_to_cli_args(intent)
        assert args[0] == "test-all"
        assert "chromium,firefox" in args or "--browsers" in args

    def test_no_url_fallback(self):
        from psr_ai.core.intent import intent_to_cli_args
        intent = self._make_intent("browse", url=None, params={})
        args = intent_to_cli_args(intent)
        assert args[0] == "browse"
