import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "etc"))
import prepare_parquet_backend as ppb


def test_parse_selections_accepts_state_year_mapping():
    selections = ppb.parse_selections('{"tx":2020,"ct":2021}')
    assert selections == [
        {"state": "tx", "year": 2020, "uri": None, "label": None},
        {"state": "ct", "year": 2021, "uri": None, "label": None},
    ]


def test_parse_selections_accepts_explicit_uri():
    selections = ppb.parse_selections(
        '[{"state":"TX","year":"2020","uri":"s3://bucket/index.parquet"}]'
    )
    assert selections[0]["state"] == "tx"
    assert selections[0]["year"] == 2020
    assert selections[0]["uri"] == "s3://bucket/index.parquet"


@pytest.mark.parametrize("raw", ["", "[]", '{"texas":2020}', '{"tx":"bad"}'])
def test_parse_selections_rejects_invalid_values(raw):
    with pytest.raises((ValueError, TypeError)):
        ppb.parse_selections(raw)


def test_split_s3_uri():
    assert ppb.split_s3_uri("s3://bucket/path/data.parquet") == (
        "bucket",
        "path/data.parquet",
    )
