"""Tests for psr_ai.core.species."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

INAT_RESPONSE = {
    "results": [
        {
            "name": "Phasma gigas",
            "preferred_common_name": "giant stick insect",
            "rank": "species",
            "observations_count": 42,
            "wikipedia_url": "https://en.wikipedia.org/wiki/Phasma_gigas",
            "id": 123456,
            "default_photo": {"medium_url": "https://example.com/photo.jpg"},
        }
    ]
}

GBIF_RESPONSE = {
    "results": [
        {
            "scientificName": "Phasma gigas Linnaeus",
            "canonicalName": "Phasma gigas",
            "rank": "SPECIES",
            "taxonomicStatus": "ACCEPTED",
            "kingdom": "Animalia",
            "phylum": "Arthropoda",
            "order": "Phasmatodea",
            "family": "Phasmatidae",
            "genus": "Phasma",
            "key": 7777777,
        }
    ]
}


def _fake_get_json(url: str, params=None, timeout=20):
    if "inaturalist.org" in url:
        return INAT_RESPONSE
    if "gbif.org" in url:
        return GBIF_RESPONSE
    return {"results": []}


# ---------------------------------------------------------------------------
# _inat_search
# ---------------------------------------------------------------------------

class TestInatSearch:
    def test_returns_species_name(self):
        from psr_ai.core.species import _inat_search

        with patch("psr_ai.core.species._get_json", side_effect=_fake_get_json):
            results = _inat_search("stick insect")

        assert results[0]["name"] == "Phasma gigas"

    def test_returns_common_name(self):
        from psr_ai.core.species import _inat_search

        with patch("psr_ai.core.species._get_json", side_effect=_fake_get_json):
            results = _inat_search("stick insect")

        assert results[0]["preferred_common_name"] == "giant stick insect"

    def test_includes_inat_url(self):
        from psr_ai.core.species import _inat_search

        with patch("psr_ai.core.species._get_json", side_effect=_fake_get_json):
            results = _inat_search("stick insect")

        assert "inaturalist.org" in results[0]["inat_url"]


# ---------------------------------------------------------------------------
# _gbif_search
# ---------------------------------------------------------------------------

class TestGbifSearch:
    def test_returns_canonical_name(self):
        from psr_ai.core.species import _gbif_search

        with patch("psr_ai.core.species._get_json", side_effect=_fake_get_json):
            results = _gbif_search("Phasma gigas")

        assert results[0]["canonicalName"] == "Phasma gigas"

    def test_includes_gbif_url(self):
        from psr_ai.core.species import _gbif_search

        with patch("psr_ai.core.species._get_json", side_effect=_fake_get_json):
            results = _gbif_search("Phasma gigas")

        assert "gbif.org" in results[0]["gbif_url"]

    def test_returns_taxonomy_fields(self):
        from psr_ai.core.species import _gbif_search

        with patch("psr_ai.core.species._get_json", side_effect=_fake_get_json):
            results = _gbif_search("Phasma gigas")

        r = results[0]
        assert r["order"] == "Phasmatodea"
        assert r["family"] == "Phasmatidae"


# ---------------------------------------------------------------------------
# lookup_species
# ---------------------------------------------------------------------------

class TestLookupSpecies:
    def test_all_sources_queried_by_default(self):
        from psr_ai.core.species import lookup_species

        with patch("psr_ai.core.species._inat_search", return_value=[{"name": "A"}]) as m_inat, \
             patch("psr_ai.core.species._gbif_search", return_value=[{"scientificName": "B"}]) as m_gbif, \
             patch("psr_ai.core.species._bugguide_search", return_value=[{"title": "C", "url": "u"}]) as m_bug:
            result = lookup_species("test")

        m_inat.assert_called_once()
        m_gbif.assert_called_once()
        m_bug.assert_called_once()
        assert result.total == 3

    def test_sources_filter_respected(self):
        from psr_ai.core.species import lookup_species

        with patch("psr_ai.core.species._inat_search", return_value=[{"name": "A"}]) as m_inat, \
             patch("psr_ai.core.species._gbif_search", return_value=[]) as m_gbif, \
             patch("psr_ai.core.species._bugguide_search", return_value=[]) as m_bug:
            result = lookup_species("test", sources=["inat"])

        m_inat.assert_called_once()
        m_gbif.assert_not_called()
        m_bug.assert_not_called()

    def test_saves_json_when_output_dir_given(self, tmp_path):
        from psr_ai.core.species import lookup_species

        with patch("psr_ai.core.species._inat_search", return_value=[{"name": "A"}]), \
             patch("psr_ai.core.species._gbif_search", return_value=[]), \
             patch("psr_ai.core.species._bugguide_search", return_value=[]):
            result = lookup_species("test query", output_dir=tmp_path)

        json_files = list(tmp_path.glob("*_species.json"))
        assert json_files
        data = json.loads(json_files[0].read_text())
        assert data["query"] == "test query"

    def test_no_json_saved_without_output_dir(self, tmp_path):
        from psr_ai.core.species import lookup_species

        with patch("psr_ai.core.species._inat_search", return_value=[]), \
             patch("psr_ai.core.species._gbif_search", return_value=[]), \
             patch("psr_ai.core.species._bugguide_search", return_value=[]):
            result = lookup_species("test", output_dir=None)

        assert result.output_dir is None

    def test_report_contains_query(self):
        from psr_ai.core.species import lookup_species

        with patch("psr_ai.core.species._inat_search", return_value=[{"name": "A", "preferred_common_name": "", "rank": "", "observations_count": 0, "wikipedia_url": "", "inat_url": "", "default_photo": ""}]), \
             patch("psr_ai.core.species._gbif_search", return_value=[]), \
             patch("psr_ai.core.species._bugguide_search", return_value=[]):
            result = lookup_species("stick insect")

        assert "stick insect" in result.report()

    def test_error_captured_gracefully(self):
        from psr_ai.core.species import lookup_species

        with patch("psr_ai.core.species._inat_search", side_effect=Exception("network failure")):
            result = lookup_species("broken query")

        assert result.error is not None
        assert "network failure" in result.error


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestSpeciesCLI:
    def test_scrape_species_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scrape-species", "--help"])
        assert result.exit_code == 0

    def test_scrape_species_help_mentions_sources(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scrape-species", "--help"])
        assert "sources" in result.output.lower()

    def test_scrape_species_runs_with_mocked_lookup(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        mock_result = MagicMock()
        mock_result.report.return_value = "Species results for: stick insect"
        mock_result.error = None

        with patch("psr_ai.core.species.lookup_species", return_value=mock_result):
            runner = CliRunner()
            result = runner.invoke(main, ["scrape-species", "stick insect"])

        assert result.exit_code == 0
        assert "stick insect" in result.output
