"""Unit tests for utils/encoding.py - the MHT charset detection pipeline."""

import pytest
from psr_ai.utils.encoding import decode_mht_bytes, _extract_declared_charset


def test_decode_plain_utf8():
    data = "Hello, world!".encode("utf-8")
    assert decode_mht_bytes(data) == "Hello, world!"


def test_decode_utf8_bom():
    # PSR sometimes prepends a BOM; utf-8-sig must strip it cleanly.
    # Encode the plain string as utf-8-sig so the codec adds the BOM at the byte level.
    data = "Step 1: Open".encode("utf-8-sig")
    # The raw bytes start with the BOM sequence EF BB BF.
    assert data[:3] == b"\xef\xbb\xbf"
    result = decode_mht_bytes(data)
    # After decoding the BOM should be absent from the result string.
    assert result == "Step 1: Open"
    assert not result.startswith("﻿")


def test_decode_windows1252():
    # Windows-1252 byte 0x92 is the right single quotation mark - not valid UTF-8.
    data = b"It\x92s a test"
    result = decode_mht_bytes(data)
    assert "test" in result


def test_decode_with_declared_charset():
    header = b"Content-Type: text/html; charset=windows-1252\r\n\r\n"
    body = "Caf\xe9".encode("windows-1252")
    result = decode_mht_bytes(header + body)
    assert "Caf" in result


def test_extract_declared_charset_found():
    data = b"content-type: text/html; charset=utf-8\r\n"
    assert _extract_declared_charset(data) == "utf-8"


def test_extract_declared_charset_missing():
    data = b"content-type: text/html\r\n"
    assert _extract_declared_charset(data) is None


def test_decode_falls_back_on_unknown_charset():
    # A declared charset that does not exist should not raise; fallback list takes over.
    header = b"Content-Type: text/html; charset=not-a-real-codec\r\n\r\n"
    body = b"Safe ASCII content"
    result = decode_mht_bytes(header + body)
    assert "Safe ASCII content" in result
