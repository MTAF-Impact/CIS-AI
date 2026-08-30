from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import RiskLevel
from app.services import risk_engine


class TestCalculateRiskScore:
    def test_matches_the_documented_formula(self):
        # Risk = 0.35*Growth + 0.25*Outrage + 0.15*Geo + 0.25*FaultLineRelevance
        score = risk_engine.calculate_risk_score(
            growth_velocity=0.8,
            emotional_intensity=0.6,
            geographic_concentration=0.4,
            fault_line_relevance=0.2,
        )
        expected = 0.35 * 0.8 + 0.25 * 0.6 + 0.15 * 0.4 + 0.25 * 0.2
        assert score == pytest.approx(expected, abs=1e-4)

    def test_all_zeros_is_zero(self):
        assert risk_engine.calculate_risk_score(0, 0, 0, 0) == 0.0

    def test_all_ones_is_one(self):
        assert risk_engine.calculate_risk_score(1, 1, 1, 1) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "inputs",
        [
            (1.5, 0, 0, 0),  # out-of-range growth
            (0, -0.5, 0, 0),  # negative outrage
            (0, 0, 2.0, 0),
        ],
    )
    def test_output_is_always_clamped_to_unit_interval(self, inputs):
        score = risk_engine.calculate_risk_score(*inputs)
        assert 0.0 <= score <= 1.0


class TestDetermineRiskLevel:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (0.0, RiskLevel.LOW),
            (0.39, RiskLevel.LOW),
            (0.4, RiskLevel.MEDIUM),
            (0.55, RiskLevel.MEDIUM),
            (0.7, RiskLevel.MEDIUM),
            (0.71, RiskLevel.HIGH),
            (1.0, RiskLevel.HIGH),
        ],
    )
    def test_boundaries(self, score, expected):
        assert risk_engine.determine_risk_level(score) == expected


class TestComputeGrowthVelocity:
    def test_empty_timestamps_is_zero(self):
        assert risk_engine.compute_growth_velocity([]) == 0.0

    def test_all_recent_is_one(self):
        now = datetime.now(UTC)
        timestamps = [now, now - timedelta(minutes=5), now - timedelta(hours=1)]
        assert risk_engine.compute_growth_velocity(timestamps, now=now) == 1.0

    def test_mixed_recency_is_fraction_recent(self):
        now = datetime.now(UTC)
        timestamps = [now, now - timedelta(hours=1), now - timedelta(hours=20)]
        # 2 of 3 posts fall within the default 6h window
        assert risk_engine.compute_growth_velocity(timestamps, now=now) == pytest.approx(
            2 / 3, abs=1e-4
        )

    def test_handles_naive_datetimes_without_raising(self):
        now = datetime.now(UTC)
        naive_now = now.replace(tzinfo=None)
        result = risk_engine.compute_growth_velocity([naive_now], now=now)
        assert result == 1.0


class TestComputeEmotionalIntensity:
    def test_averages_non_null_scores(self):
        assert risk_engine.compute_emotional_intensity([0.8, 0.6, None]) == pytest.approx(0.7)

    def test_all_none_is_zero(self):
        assert risk_engine.compute_emotional_intensity([None, None]) == 0.0

    def test_empty_is_zero(self):
        assert risk_engine.compute_emotional_intensity([]) == 0.0


class TestComputeGeographicConcentration:
    def test_all_same_location_is_one(self):
        assert risk_engine.compute_geographic_concentration(["A", "A", "A"]) == 1.0

    def test_split_locations_is_fraction_of_majority(self):
        assert risk_engine.compute_geographic_concentration(["A", "A", "B"]) == pytest.approx(
            2 / 3, abs=1e-4
        )

    def test_ignores_none_locations(self):
        assert risk_engine.compute_geographic_concentration(["A", None, "A"]) == 1.0

    def test_no_locations_is_zero(self):
        assert risk_engine.compute_geographic_concentration([None, None]) == 0.0


class TestComputeFaultLineRelevance:
    def test_no_fault_lines_is_zero_with_no_matches(self):
        import numpy as np

        centroid = np.array([1.0, 0.0, 0.0])
        score, matched = risk_engine.compute_fault_line_relevance(centroid, [])
        assert score == 0.0
        assert matched == []

    def test_identical_vector_matches_with_max_similarity(self):
        import numpy as np

        centroid = np.array([1.0, 0.0, 0.0])
        fault_lines = [("fl-1", [1.0, 0.0, 0.0]), ("fl-2", [0.0, 1.0, 0.0])]
        score, matched = risk_engine.compute_fault_line_relevance(centroid, fault_lines)
        assert score == pytest.approx(1.0)
        assert matched == ["fl-1"]

    def test_orthogonal_vectors_do_not_match(self):
        import numpy as np

        centroid = np.array([1.0, 0.0, 0.0])
        fault_lines = [("fl-1", [0.0, 1.0, 0.0])]
        score, matched = risk_engine.compute_fault_line_relevance(centroid, fault_lines)
        assert score == pytest.approx(0.0, abs=1e-6)
        assert matched == []
