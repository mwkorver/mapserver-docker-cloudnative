"""
Unit tests for etc/mapfile_generator.py.

All tests are pure Python — no GDAL, no S3, no running container required.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "etc"))
import mapfile_generator as mg


# ---------------------------------------------------------------------------
# tileindex_groups
# ---------------------------------------------------------------------------

class TestTileindexGroups:
    def _collection_single(self):
        return {
            "id": "ky-2024",
            "native_epsg": 26917,
            "tileindex": "/usr/src/mapfiles/ky-2024_tileindex.fgb",
            "tileindex_layer_name": "ky-2024_tileindex_26917",
            "layer_name": "ky-2024",
            "extent_native": [100000, 200000, 500000, 600000],
        }

    def _collection_multi(self):
        return {
            "id": "mixed",
            "native_epsg": 26917,
            "tileindexes": [
                {
                    "epsg": 26917,
                    "tileindex": "/usr/src/mapfiles/mixed_tileindex_26917.fgb",
                    "tileindex_layer_name": "mixed_tileindex_26917",
                    "layer_name": "mixed-26917",
                    "extent_native": [0, 0, 100, 100],
                    "cog_count": 5,
                },
                {
                    "epsg": 26918,
                    "tileindex": "/usr/src/mapfiles/mixed_tileindex_26918.fgb",
                    "tileindex_layer_name": "mixed_tileindex_26918",
                    "layer_name": "mixed-26918",
                    "extent_native": [200, 0, 300, 100],
                    "cog_count": 3,
                },
            ],
        }

    def test_single_epsg_returns_one_group(self):
        groups = mg.tileindex_groups(self._collection_single())
        assert len(groups) == 1
        assert groups[0]["epsg"] == 26917

    def test_single_epsg_synthesises_from_top_level_fields(self):
        groups = mg.tileindex_groups(self._collection_single())
        g = groups[0]
        assert g["tileindex"] == "/usr/src/mapfiles/ky-2024_tileindex.fgb"
        assert g["layer_name"] == "ky-2024"

    def test_multi_epsg_returns_tileindexes_array(self):
        groups = mg.tileindex_groups(self._collection_multi())
        assert len(groups) == 2
        epsgs = {g["epsg"] for g in groups}
        assert epsgs == {26917, 26918}

    def test_empty_tileindexes_falls_back_to_synthesis(self):
        """An explicit empty list should still produce one synthetic group."""
        c = self._collection_single()
        c["tileindexes"] = []
        groups = mg.tileindex_groups(c)
        assert len(groups) == 1


# ---------------------------------------------------------------------------
# tileindex_map_layer_name
# ---------------------------------------------------------------------------

class TestTileindexMapLayerName:
    def test_single_epsg_no_suffix(self):
        c = {
            "id": "ky-2024",
            "native_epsg": 26917,
            "tileindex": "/x/ky-2024_tileindex.fgb",
        }
        groups = mg.tileindex_groups(c)
        name = mg.tileindex_map_layer_name(c, groups[0])
        assert name == "cog-tileindex-ky-2024"
        assert "26917" not in name

    def test_multi_epsg_adds_suffix(self):
        c = {
            "id": "mixed",
            "tileindexes": [
                {"epsg": 26917, "tileindex": "/x/a.fgb", "layer_name": "mixed-26917"},
                {"epsg": 26918, "tileindex": "/x/b.fgb", "layer_name": "mixed-26918"},
            ],
        }
        groups = mg.tileindex_groups(c)
        names = [mg.tileindex_map_layer_name(c, g) for g in groups]
        assert "cog-tileindex-mixed-26917" in names
        assert "cog-tileindex-mixed-26918" in names

    def test_none_group_uses_collection_id(self):
        c = {
            "id": "ky-2024",
            "native_epsg": 26917,
            "tileindex": "/x/ky-2024_tileindex.fgb",
        }
        name = mg.tileindex_map_layer_name(c, None)
        assert name == "cog-tileindex-ky-2024"


# ---------------------------------------------------------------------------
# raster_layer_for_group
# ---------------------------------------------------------------------------

class TestRasterLayerForGroup:
    def _single_group(self, layer_name="ky-2024", epsg=26917):
        return {
            "epsg": epsg,
            "tileindex": "/x/ky.fgb",
            "layer_name": layer_name,
            "extent_native": [0, 0, 1, 1],
        }

    def test_default_processing_bands_and_resample(self):
        c = {"id": "ky-2024", "label": "Kentucky", "native_epsg": 26917,
             "tileindex": "/x/ky.fgb"}
        group = self._single_group()
        lines = mg.raster_layer_for_group(c, group)
        joined = "\n".join(lines)
        assert 'PROCESSING "BANDS=1,2,3"' in joined
        assert 'PROCESSING "RESAMPLE=AVERAGE"' in joined

    def test_custom_raster_processing_override(self):
        c = {
            "id": "nj-2020",
            "label": "NJ 2020",
            "native_epsg": 26918,
            "tileindex": "/x/nj.fgb",
            "raster_processing": ["BANDS=1", "SCALE=0,65535", "USE_MASK_BAND=NO"],
        }
        group = self._single_group("nj-2020", 26918)
        lines = mg.raster_layer_for_group(c, group)
        joined = "\n".join(lines)
        assert 'PROCESSING "BANDS=1"' in joined
        assert 'PROCESSING "SCALE=0,65535"' in joined
        assert 'PROCESSING "USE_MASK_BAND=NO"' in joined
        # Default bands must NOT appear
        assert "BANDS=1,2,3" not in joined

    def test_layer_name_from_group(self):
        c = {"id": "col", "label": "Col", "native_epsg": 26917,
             "tileindex": "/x/col.fgb"}
        group = self._single_group(layer_name="col-26917")
        lines = mg.raster_layer_for_group(c, group)
        assert any('NAME "col-26917"' in l for l in lines)

    def test_tileindex_references_map_layer_name(self):
        c = {
            "id": "col",
            "label": "Col",
            "native_epsg": 26917,
            "tileindex": "/x/col.fgb",
        }
        groups = mg.tileindex_groups(c)
        layer_lines = mg.raster_layer_for_group(c, groups[0])
        tileindex_line = next(l for l in layer_lines if "TILEINDEX" in l)
        expected_map_name = mg.tileindex_map_layer_name(c, groups[0])
        assert expected_map_name in tileindex_line


# ---------------------------------------------------------------------------
# db_connection_string
# ---------------------------------------------------------------------------

class TestDbConnectionString:
    def test_returns_none_without_db_host(self, monkeypatch):
        monkeypatch.delenv("DB_HOST", raising=False)
        monkeypatch.delenv("DB_USER", raising=False)
        assert mg.db_connection_string() is None

    def test_returns_none_without_db_user(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "myhost")
        monkeypatch.delenv("DB_USER", raising=False)
        assert mg.db_connection_string() is None

    def test_returns_string_with_both(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "myhost")
        monkeypatch.setenv("DB_USER", "mapserver")
        monkeypatch.delenv("DB_PASS", raising=False)
        monkeypatch.delenv("DB_PASSWORD", raising=False)
        conn = mg.db_connection_string()
        assert "host=myhost" in conn
        assert "user=mapserver" in conn

    def test_prefers_db_pass_over_db_password(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "h")
        monkeypatch.setenv("DB_USER", "u")
        monkeypatch.setenv("DB_PASS", "legacy_pass")
        monkeypatch.setenv("DB_PASSWORD", "cdk_pass")
        conn = mg.db_connection_string()
        assert "legacy_pass" in conn
        assert "cdk_pass" not in conn

    def test_falls_back_to_db_password(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "h")
        monkeypatch.setenv("DB_USER", "u")
        monkeypatch.delenv("DB_PASS", raising=False)
        monkeypatch.setenv("DB_PASSWORD", "cdk_pass")
        conn = mg.db_connection_string()
        assert "cdk_pass" in conn

    def test_default_port_and_dbname(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "h")
        monkeypatch.setenv("DB_USER", "u")
        monkeypatch.delenv("DB_PORT", raising=False)
        monkeypatch.delenv("DB_NAME", raising=False)
        monkeypatch.delenv("DB_PASS", raising=False)
        monkeypatch.delenv("DB_PASSWORD", raising=False)
        conn = mg.db_connection_string()
        assert "port=5432" in conn
        assert "dbname=mapserver" in conn


# ---------------------------------------------------------------------------
# ogr_layer_name
# ---------------------------------------------------------------------------

class TestOgrLayerName:
    def test_geojson_with_name_field(self, tmp_path):
        p = tmp_path / "test.geojson"
        p.write_text('{"type":"FeatureCollection","name":"my_layer","features":[]}')
        assert mg.ogr_layer_name(str(p)) == "my_layer"

    def test_geojson_without_name_falls_back_to_stem(self, tmp_path):
        p = tmp_path / "my_file.geojson"
        p.write_text('{"type":"FeatureCollection","features":[]}')
        assert mg.ogr_layer_name(str(p)) == "my_file"

    def test_fgb_uses_stem_without_parsing(self, tmp_path):
        # FGB is binary — ogr_layer_name must NOT attempt json.load on it.
        p = tmp_path / "ky_tileindex_26917.fgb"
        p.write_bytes(b"\x00\x01\x02binary content")
        assert mg.ogr_layer_name(str(p)) == "ky_tileindex_26917"

    def test_geojson_missing_file_uses_stem(self):
        assert mg.ogr_layer_name("/nonexistent/path/layer.geojson") == "layer"
