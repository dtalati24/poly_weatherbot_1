"""Tests for forecast harvesting and archival.

These cover the properties that make archived data trustworthy: records round
trip losslessly, they carry the leakage anchor, writes are atomic, and reruns
are idempotent. No network access -- payloads are synthetic.
"""

from __future__ import annotations

import gzip
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.harvest import (  # noqa: E402
    SCHEMA_VERSION,
    archive_path,
    build_record,
    harvest_ensemble_model,
    read_record,
    write_record,
)
from weatherbot.sources import openmeteo  # noqa: E402

MOMENT = datetime(2026, 8, 1, 12, 49, 5, tzinfo=timezone.utc)


def make_payload(n_members: int = 3, n_steps: int = 4) -> dict:
    hourly = {"time": [f"2026-08-01T{h:02d}:00" for h in range(n_steps)]}
    hourly["temperature_2m"] = [20.0 + i for i in range(n_steps)]
    for m in range(1, n_members):
        hourly[f"temperature_2m_member{m:02d}"] = [
            20.0 + i + m * 0.1 for i in range(n_steps)
        ]
    return {"latitude": 51.5, "longitude": 0.05, "elevation": 4.0, "hourly": hourly}


class TestArchivePath:
    def test_hour_resolution_makes_reruns_idempotent(self, tmp_path):
        later = MOMENT.replace(minute=59, second=59)
        assert archive_path("m", MOMENT, tmp_path) == archive_path("m", later, tmp_path)

    def test_different_hours_are_distinct(self, tmp_path):
        other = MOMENT.replace(hour=13)
        assert archive_path("m", MOMENT, tmp_path) != archive_path("m", other, tmp_path)

    def test_layout_partitions_by_model_and_year(self, tmp_path):
        path = archive_path("ecmwf_ifs025", MOMENT, tmp_path)
        assert path.parent == tmp_path / "ecmwf_ifs025" / "2026"
        assert path.name == "2026-08-01T12Z.json.gz"


class TestRecord:
    def test_carries_leakage_anchor(self):
        record = build_record("m", make_payload(), MOMENT)
        assert record["harvested_at_utc"] == "2026-08-01T12:49:05Z"
        assert record["schema_version"] == SCHEMA_VERSION

    def test_payload_is_preserved_verbatim(self):
        payload = make_payload()
        record = build_record("m", payload, MOMENT)
        assert record["payload"] == payload

    def test_round_trips_through_disk(self, tmp_path):
        record = build_record("m", make_payload(), MOMENT)
        path = tmp_path / "r.json.gz"
        write_record(record, path)
        assert read_record(path) == record

    def test_write_is_deterministic(self, tmp_path):
        """Identical content must produce identical bytes, or every rerun
        creates a spurious git diff in the archive."""
        record = build_record("m", make_payload(), MOMENT)
        a, b = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
        write_record(record, a)
        write_record(record, b)
        assert a.read_bytes() == b.read_bytes()

    def test_no_temp_file_left_behind(self, tmp_path):
        path = tmp_path / "r.json.gz"
        write_record(build_record("m", make_payload(), MOMENT), path)
        assert list(tmp_path.iterdir()) == [path]

    def test_output_is_valid_gzip_json(self, tmp_path):
        path = tmp_path / "r.json.gz"
        write_record(build_record("m", make_payload(), MOMENT), path)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            assert json.load(fh)["model"] == "m"


class TestExtractMembers:
    def test_counts_members_including_unperturbed(self):
        series = openmeteo.extract_members(make_payload(n_members=5))
        assert series.n_members == 5
        assert len(series.times) == 4

    def test_all_null_series_are_dropped(self):
        payload = make_payload(n_members=2)
        payload["hourly"]["temperature_2m_member09"] = [None] * 4
        assert openmeteo.extract_members(payload).n_members == 2

    def test_empty_payload(self):
        assert openmeteo.extract_members({}).n_members == 0


class TestLeadVariables:
    def test_base_variable_comes_first(self):
        names = openmeteo.lead_variables("temperature_2m", (1, 2, 3))
        assert names[0] == "temperature_2m"
        assert names[1:] == [
            "temperature_2m_previous_day1",
            "temperature_2m_previous_day2",
            "temperature_2m_previous_day3",
        ]

    def test_no_leads(self):
        assert openmeteo.lead_variables("temperature_2m", ()) == ["temperature_2m"]


class TestHarvestEnsembleModel:
    def _model(self, members: int = 3) -> openmeteo.EnsembleModel:
        return openmeteo.EnsembleModel("fake_model", members, 7.0)

    def test_writes_and_reports(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            openmeteo, "fetch_ensemble", lambda *a, **k: make_payload(3)
        )
        result = harvest_ensemble_model(self._model(3), MOMENT, root=tmp_path)
        assert result.ok and not result.skipped
        assert result.n_members == 3
        assert result.path.exists()

    def test_second_run_same_hour_is_skipped(self, tmp_path, monkeypatch):
        calls = []

        def fake(*a, **k):
            calls.append(1)
            return make_payload(3)

        monkeypatch.setattr(openmeteo, "fetch_ensemble", fake)
        harvest_ensemble_model(self._model(), MOMENT, root=tmp_path)
        second = harvest_ensemble_model(self._model(), MOMENT, root=tmp_path)
        assert second.skipped
        assert len(calls) == 1, "skipped run must not hit the network"

    def test_overwrite_forces_refetch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            openmeteo, "fetch_ensemble", lambda *a, **k: make_payload(3)
        )
        harvest_ensemble_model(self._model(), MOMENT, root=tmp_path)
        again = harvest_ensemble_model(
            self._model(), MOMENT, root=tmp_path, overwrite=True
        )
        assert not again.skipped and again.ok

    def test_member_count_change_is_flagged(self, tmp_path, monkeypatch):
        """A feed silently changing shape would quietly corrupt the archive."""
        monkeypatch.setattr(
            openmeteo, "fetch_ensemble", lambda *a, **k: make_payload(3)
        )
        result = harvest_ensemble_model(self._model(members=51), MOMENT, root=tmp_path)
        assert result.ok
        assert result.member_warning is not None
        assert "51" in result.member_warning and "3" in result.member_warning

    def test_network_failure_is_returned_not_raised(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise openmeteo.OpenMeteoError("upstream down")

        monkeypatch.setattr(openmeteo, "fetch_ensemble", boom)
        result = harvest_ensemble_model(self._model(), MOMENT, root=tmp_path)
        assert not result.ok
        assert "upstream down" in result.error
        assert result.status == "FAILED"

    def test_empty_payload_is_a_failure_not_a_silent_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            openmeteo,
            "fetch_ensemble",
            lambda *a, **k: {"hourly": {"time": [], "temperature_2m": []}},
        )
        result = harvest_ensemble_model(self._model(), MOMENT, root=tmp_path)
        assert not result.ok
        assert not archive_path("fake_model", MOMENT, tmp_path).exists()


class TestModelRegistry:
    def test_primary_model_is_registered(self):
        ids = {m.model_id for m in openmeteo.ENSEMBLE_MODELS}
        assert "ecmwf_ifs025" in ids

    def test_model_ids_are_unique(self):
        ids = [m.model_id for m in openmeteo.ENSEMBLE_MODELS]
        assert len(ids) == len(set(ids))

    def test_ukmo_2km_is_documented_as_truncated(self):
        """This must stay visible -- it is why the Met Office route matters."""
        model = next(
            m for m in openmeteo.ENSEMBLE_MODELS if m.model_id == "ukmo_uk_ensemble_2km"
        )
        assert model.members < 5
        assert "MOGREPS-UK" in model.note


@pytest.mark.parametrize("n_members", [1, 3, 51])
def test_extract_members_scales(n_members):
    series = openmeteo.extract_members(make_payload(n_members=n_members))
    assert series.n_members == n_members
