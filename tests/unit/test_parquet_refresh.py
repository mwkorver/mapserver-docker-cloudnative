import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "etc"))
import parquet_refresh as pr


def configure_paths(monkeypatch, tmp_path):
    mapfiles = tmp_path / "mapfiles"
    monkeypatch.setattr(pr, "MAPFILES_DIR", mapfiles)
    monkeypatch.setattr(pr, "COLLECTIONS_FILE", mapfiles / "collections.json")
    monkeypatch.setattr(pr, "MAPFILE", mapfiles / "mapfile.map")
    monkeypatch.setattr(pr, "GENERATIONS_DIR", mapfiles / "parquet-generations")
    monkeypatch.setattr(pr, "STATUS_FILE", mapfiles / "parquet-refresh.json")
    return mapfiles


def test_selection_document_uses_environment(monkeypatch):
    monkeypatch.delenv("PARQUET_SELECTION_S3_URI", raising=False)
    monkeypatch.setenv("PARQUET_SELECTION_JSON", '{"tx":2020}')
    raw, source = pr.selection_document()
    assert json.loads(raw) == {"tx": 2020}
    assert source == {"type": "environment"}


def test_generation_paths_only_returns_managed_directories(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    managed = pr.GENERATIONS_DIR / "old"
    doc = {
        "collections": [
            {"tileindexes": [{"tileindex": str(managed / "tx.parquet")}]},
            {"tileindexes": [{"tileindex": "/tmp/outside.parquet"}]},
        ]
    }
    assert pr.generation_paths(doc) == {managed.resolve()}


def test_refresh_atomically_switches_generation(monkeypatch, tmp_path):
    mapfiles = configure_paths(monkeypatch, tmp_path)
    old_generation = pr.GENERATIONS_DIR / "old"
    old_generation.mkdir(parents=True)
    (old_generation / "old.parquet").write_text("old")
    mapfiles.mkdir(exist_ok=True)
    pr.COLLECTIONS_FILE.write_text(
        json.dumps(
            {
                "collections": [
                    {
                        "tileindexes": [
                            {"tileindex": str(old_generation / "old.parquet")}
                        ]
                    }
                ]
            }
        )
    )
    pr.MAPFILE.write_text("old map")
    monkeypatch.setattr(
        pr,
        "selection_document",
        lambda: ('{"tx":2020}', {"type": "environment"}),
    )

    def fake_run(command, env=None):
        if command[-1].endswith("prepare_parquet_backend.py"):
            generation = Path(env["PARQUET_INDEX_DIR"])
            staged = generation / "naip-tx-2020.parquet"
            staged.write_text("new")
            Path(env["COLLECTIONS_FILE"]).write_text(
                json.dumps(
                    {
                        "collections": [
                            {
                                "id": "naip-tx-2020",
                                "tileindexes": [{"tileindex": str(staged)}],
                            }
                        ]
                    }
                )
            )
        elif command[-1].endswith("mapfile_generator.py"):
            Path(env["MAPFILE_OUTPUT"]).write_text("new map")
        return "ok"

    monkeypatch.setattr(pr, "run_checked", fake_run)
    result = pr.refresh(restart_workers=False)

    catalog = json.loads(pr.COLLECTIONS_FILE.read_text())
    active_path = Path(catalog["collections"][0]["tileindexes"][0]["tileindex"])
    assert active_path.exists()
    assert pr.MAPFILE.read_text() == "new map"
    assert not old_generation.exists()
    assert result["collections"] == 1
    assert json.loads(pr.STATUS_FILE.read_text())["status"] == "ready"


def test_refresh_restores_previous_files_when_restart_fails(monkeypatch, tmp_path):
    mapfiles = configure_paths(monkeypatch, tmp_path)
    old_generation = pr.GENERATIONS_DIR / "old"
    old_generation.mkdir(parents=True)
    old_index = old_generation / "old.parquet"
    old_index.write_text("old")
    old_catalog = {
        "collections": [{"tileindexes": [{"tileindex": str(old_index)}]}]
    }
    pr.COLLECTIONS_FILE.write_text(json.dumps(old_catalog))
    pr.MAPFILE.write_text("old map")
    monkeypatch.setattr(
        pr,
        "selection_document",
        lambda: ('{"tx":2020}', {"type": "environment"}),
    )

    def fake_run(command, env=None):
        if command[-1].endswith("prepare_parquet_backend.py"):
            generation = Path(env["PARQUET_INDEX_DIR"])
            staged = generation / "new.parquet"
            staged.write_text("new")
            Path(env["COLLECTIONS_FILE"]).write_text(
                json.dumps(
                    {"collections": [{"tileindexes": [{"tileindex": str(staged)}]}]}
                )
            )
        elif command[-1].endswith("mapfile_generator.py"):
            Path(env["MAPFILE_OUTPUT"]).write_text("new map")
        return "ok"

    starts = 0

    def fake_supervisor(action):
        nonlocal starts
        if action == "start":
            starts += 1
            if starts == 1:
                raise RuntimeError("start failed")
        return action

    monkeypatch.setattr(pr, "run_checked", fake_run)
    monkeypatch.setattr(pr, "supervisor", fake_supervisor)

    try:
        pr.refresh(restart_workers=True)
        assert False, "refresh should fail"
    except RuntimeError as exc:
        assert "start failed" in str(exc)

    assert json.loads(pr.COLLECTIONS_FILE.read_text()) == old_catalog
    assert pr.MAPFILE.read_text() == "old map"
    assert old_generation.exists()
    assert starts == 2
