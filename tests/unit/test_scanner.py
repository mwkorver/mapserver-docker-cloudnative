"""
Unit tests for scripts/scan_cog_collection.py.

Pure-Python logic (grouping, bbox, upsert) runs on any machine.
GDAL-dependent tests (write_tileindex_fgb) are skipped when osgeo is not
installed; they always run inside the Docker container where GDAL is present.
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Import the scanner, stubbing out osgeo if it is not installed.
# ---------------------------------------------------------------------------
_GDAL_AVAILABLE = True
try:
    from osgeo import gdal as _real_gdal  # noqa: F401
except ImportError:
    _GDAL_AVAILABLE = False
    for _mod in ("osgeo", "osgeo.gdal", "osgeo.osr", "osgeo.ogr"):
        sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import importlib as _il
scanner = _il.import_module("scan_cog_collection")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_feature(key, epsg, x0=0.0, y0=0.0, x1=1.0, y1=1.0):
    """Minimal GeoJSON feature matching the scanner's output shape."""
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {
        "type": "Feature",
        "properties": {
            "key": key,
            "location": f"/vsicurl/http://localhost:8001/standard/us-east-1/bucket/{key}",
            "file_name": key.split("/")[-1],
            "epsg": epsg,
            "collection": "test",
            "bucket": "bucket",
            "width": 100,
            "height": 100,
            "bands": 3,
            "data_type": "Byte",
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


# ---------------------------------------------------------------------------
# group_features_by_epsg
# ---------------------------------------------------------------------------

class TestGroupFeaturesByEpsg:
    def test_single_epsg(self):
        features = [make_feature(f"tile{i}.tif", 26917) for i in range(4)]
        native_by, web_by = scanner.group_features_by_epsg(features, features)
        assert set(native_by.keys()) == {26917}
        assert len(native_by[26917]) == 4

    def test_multi_epsg(self):
        f17 = [make_feature(f"a{i}.tif", 26917) for i in range(3)]
        f18 = [make_feature(f"b{i}.tif", 26918) for i in range(2)]
        native_by, web_by = scanner.group_features_by_epsg(f17 + f18, f17 + f18)
        assert set(native_by.keys()) == {26917, 26918}
        assert len(native_by[26917]) == 3
        assert len(native_by[26918]) == 2

    def test_none_epsg_grouped_separately(self):
        features = [make_feature("a.tif", 26917), make_feature("b.tif", None)]
        native_by, _ = scanner.group_features_by_epsg(features, features)
        assert None in native_by
        assert 26917 in native_by

    def test_web_and_native_grouped_independently(self):
        # Native features may have more rows than web (projection-edge drops).
        native = [make_feature("a.tif", 26917), make_feature("b.tif", 26918)]
        web = [make_feature("a.tif", 26917)]  # b.tif dropped
        native_by, web_by = scanner.group_features_by_epsg(native, web)
        assert len(native_by[26918]) == 1
        assert 26918 not in web_by


# ---------------------------------------------------------------------------
# features_bbox
# ---------------------------------------------------------------------------

class TestFeaturesBbox:
    def test_single_feature(self):
        f = make_feature("a.tif", 26917, x0=100.0, y0=200.0, x1=110.0, y1=210.0)
        bbox = scanner.features_bbox([f])
        assert bbox == pytest.approx([100.0, 200.0, 110.0, 210.0])

    def test_union_of_multiple(self):
        f1 = make_feature("a.tif", 26917, x0=0.0, y0=0.0, x1=10.0, y1=10.0)
        f2 = make_feature("b.tif", 26917, x0=5.0, y0=-5.0, x1=20.0, y1=8.0)
        bbox = scanner.features_bbox([f1, f2])
        assert bbox == pytest.approx([0.0, -5.0, 20.0, 10.0])

    def test_skips_non_finite(self):
        import math
        # All points in this ring have inf x — none contribute to the bbox.
        ring_all_inf = [
            [math.inf, 0.0], [math.inf, 1.0], [math.inf, 0.0],
        ]
        f_bad = {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [ring_all_inf]},
        }
        f_good = make_feature("ok.tif", 26917, x0=5.0, y0=5.0, x1=10.0, y1=10.0)
        bbox = scanner.features_bbox([f_bad, f_good])
        assert bbox == pytest.approx([5.0, 5.0, 10.0, 10.0])

    def test_empty_returns_none(self):
        assert scanner.features_bbox([]) is None


# ---------------------------------------------------------------------------
# upsert_collection
# ---------------------------------------------------------------------------

class TestUpsertCollection:
    def test_insert_new(self, tmp_path):
        p = tmp_path / "collections.json"
        entry = {"id": "ky-2024", "label": "Kentucky", "enabled": True, "cog_count": 42}
        scanner.upsert_collection(str(p), entry)
        doc = json.loads(p.read_text())
        assert doc["version"] == scanner.COLLECTIONS_SCHEMA_VERSION
        ids = [c["id"] for c in doc["collections"]]
        assert "ky-2024" in ids

    def test_update_existing(self, tmp_path):
        p = tmp_path / "collections.json"
        doc = {
            "version": scanner.COLLECTIONS_SCHEMA_VERSION,
            "collections": [{"id": "ky-2024", "cog_count": 10, "enabled": False}],
        }
        p.write_text(json.dumps(doc))
        scanner.upsert_collection(str(p), {"id": "ky-2024", "cog_count": 99})
        updated = json.loads(p.read_text())
        c = next(c for c in updated["collections"] if c["id"] == "ky-2024")
        assert c["cog_count"] == 99  # scanner field updated

    def test_merge_preserves_fields_not_in_entry(self, tmp_path):
        """raster_processing set by a human should survive a rescan."""
        p = tmp_path / "collections.json"
        doc = {
            "version": scanner.COLLECTIONS_SCHEMA_VERSION,
            "collections": [{
                "id": "nj-2020",
                "raster_processing": ["BANDS=1", "SCALE=0,65535"],
                "cog_count": 5,
            }],
        }
        p.write_text(json.dumps(doc))
        # Scanner entry does NOT include raster_processing
        scanner.upsert_collection(str(p), {"id": "nj-2020", "cog_count": 6})
        updated = json.loads(p.read_text())
        c = next(c for c in updated["collections"] if c["id"] == "nj-2020")
        assert c["raster_processing"] == ["BANDS=1", "SCALE=0,65535"]
        assert c["cog_count"] == 6

    def test_multiple_collections_preserved(self, tmp_path):
        p = tmp_path / "collections.json"
        doc = {
            "version": scanner.COLLECTIONS_SCHEMA_VERSION,
            "collections": [
                {"id": "ky-2024", "cog_count": 1},
                {"id": "nj-2020", "cog_count": 2},
            ],
        }
        p.write_text(json.dumps(doc))
        scanner.upsert_collection(str(p), {"id": "ky-2024", "cog_count": 99})
        updated = json.loads(p.read_text())
        assert len(updated["collections"]) == 2
        nj = next(c for c in updated["collections"] if c["id"] == "nj-2020")
        assert nj["cog_count"] == 2  # untouched

    def test_creates_file_if_missing(self, tmp_path):
        p = tmp_path / "new_dir" / "collections.json"
        p.parent.mkdir()
        scanner.upsert_collection(str(p), {"id": "fresh", "label": "Fresh"})
        doc = json.loads(p.read_text())
        assert len(doc["collections"]) == 1

    def test_version_mismatch_warns(self, tmp_path, capsys):
        p = tmp_path / "collections.json"
        p.write_text(json.dumps({"version": 99, "collections": []}))
        scanner.upsert_collection(str(p), {"id": "x"})
        captured = capsys.readouterr()
        assert "WARN" in captured.err


# ---------------------------------------------------------------------------
# load_into_postgis — call-pattern tests (no real DB)
# ---------------------------------------------------------------------------

class TestLoadIntoPostgisCallPattern:
    """Verify the delete_first flag plumbing without a live database."""

    def _mock_psycopg2(self):
        mock_cur = MagicMock()
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_pg = MagicMock()
        mock_pg.connect.return_value = mock_conn
        return mock_pg, mock_conn, mock_cur

    def test_delete_first_true_issues_delete(self, monkeypatch):
        mock_pg, mock_conn, mock_cur = self._mock_psycopg2()
        monkeypatch.setattr(scanner, "psycopg2", mock_pg)
        monkeypatch.setattr(scanner, "execute_values", MagicMock(), raising=False)
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_USER", "mapserver")

        native = [make_feature("a.tif", 26917)]
        web = [make_feature("a.tif", 26917)]
        scanner.load_into_postgis("col1", native, web, 26917, delete_first=True)

        delete_calls = [c for c in mock_cur.execute.call_args_list
                        if "DELETE" in str(c)]
        assert len(delete_calls) == 1

    def test_delete_first_false_skips_delete(self, monkeypatch):
        mock_pg, mock_conn, mock_cur = self._mock_psycopg2()
        monkeypatch.setattr(scanner, "psycopg2", mock_pg)
        monkeypatch.setattr(scanner, "execute_values", MagicMock(), raising=False)
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_USER", "mapserver")

        native = [make_feature("a.tif", 26918)]
        web = [make_feature("a.tif", 26918)]
        scanner.load_into_postgis("col1", native, web, 26918, delete_first=False)

        delete_calls = [c for c in mock_cur.execute.call_args_list
                        if "DELETE" in str(c)]
        assert len(delete_calls) == 0

    def test_multi_epsg_delete_once(self, monkeypatch):
        """Simulate the main() per-EPSG loop: DELETE fires only on first group."""
        mock_pg, mock_conn, mock_cur = self._mock_psycopg2()
        mock_execute_values = MagicMock()
        monkeypatch.setattr(scanner, "psycopg2", mock_pg)
        monkeypatch.setattr(scanner, "execute_values", mock_execute_values, raising=False)
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_USER", "mapserver")

        source_epsgs = [26917, 26918]
        native_by_epsg = {
            26917: [make_feature("a.tif", 26917)],
            26918: [make_feature("b.tif", 26918)],
        }
        web_by_epsg = {
            26917: [make_feature("a.tif", 26917)],
            26918: [make_feature("b.tif", 26918)],
        }
        for i, epsg in enumerate(source_epsgs):
            scanner.load_into_postgis(
                "test-col",
                native_by_epsg[epsg],
                web_by_epsg[epsg],
                epsg,
                delete_first=(i == 0),
            )

        delete_calls = [c for c in mock_cur.execute.call_args_list
                        if "DELETE" in str(c)]
        assert len(delete_calls) == 1  # exactly once for both groups combined
        assert mock_execute_values.call_count == 2  # one INSERT batch per group

    def test_rows_use_correct_srid_per_group(self, monkeypatch):
        """The SQL template must embed the group's EPSG, not a global one."""
        mock_pg, _, _ = self._mock_psycopg2()
        captured_templates = []

        def capture_execute_values(cur, sql, rows, template=None, page_size=500):
            captured_templates.append(template)

        monkeypatch.setattr(scanner, "psycopg2", mock_pg)
        monkeypatch.setattr(scanner, "execute_values", capture_execute_values, raising=False)
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_USER", "mapserver")

        for epsg in (26917, 26918):
            f = make_feature("x.tif", epsg)
            scanner.load_into_postgis("col", [f], [f], epsg, delete_first=False)

        assert "26917" in captured_templates[0]
        assert "26918" in captured_templates[1]
        # Critical: each group must use its own EPSG, not the other's
        assert "26918" not in captured_templates[0]
        assert "26917" not in captured_templates[1]


# ---------------------------------------------------------------------------
# write_tileindex_fgb — requires real GDAL
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _GDAL_AVAILABLE, reason="osgeo not installed")
class TestWriteTileindexFgb:
    def test_round_trip(self, tmp_path):
        from osgeo import ogr, osr as _osr

        features = [
            make_feature("tile1.tif", 26917, x0=0.0, y0=0.0, x1=1000.0, y1=1000.0),
            make_feature("tile2.tif", 26917, x0=1000.0, y0=0.0, x1=2000.0, y1=1000.0),
            make_feature("tile3.tif", 26917, x0=0.0, y0=1000.0, x1=1000.0, y1=2000.0),
        ]
        out = tmp_path / "test_tileindex.fgb"
        scanner.write_tileindex_fgb(str(out), "test_layer", 26917, features)

        assert out.exists()
        ds = ogr.Open(str(out))
        assert ds is not None
        layer = ds.GetLayer(0)
        assert layer is not None
        assert layer.GetFeatureCount() == 3

    def test_layer_name_preserved(self, tmp_path):
        from osgeo import ogr

        features = [make_feature("a.tif", 26917)]
        out = tmp_path / "named.fgb"
        scanner.write_tileindex_fgb(str(out), "my_custom_layer", 26917, features)

        ds = ogr.Open(str(out))
        layer = ds.GetLayer(0)
        assert layer.GetName() == "my_custom_layer"

    def test_atomic_write(self, tmp_path):
        """A second write should replace the first cleanly."""
        out = tmp_path / "atomic.fgb"
        scanner.write_tileindex_fgb(
            str(out), "lyr", 26917,
            [make_feature("a.tif", 26917)],
        )
        scanner.write_tileindex_fgb(
            str(out), "lyr", 26917,
            [make_feature("b.tif", 26917), make_feature("c.tif", 26917)],
        )
        from osgeo import ogr
        ds = ogr.Open(str(out))
        assert ds.GetLayer(0).GetFeatureCount() == 2

    def test_empty_features(self, tmp_path):
        """Zero features should produce a valid but empty FGB file."""
        out = tmp_path / "empty.fgb"
        scanner.write_tileindex_fgb(str(out), "empty_layer", 26917, [])
        from osgeo import ogr
        ds = ogr.Open(str(out))
        assert ds is not None
        assert ds.GetLayer(0).GetFeatureCount() == 0
