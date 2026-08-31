import uuid

import pytest

from app.services import scoring_engine as se


class TestRawReach:
    def test_all_zero_is_zero(self):
        assert se.raw_reach(0, 0, 0, 0) == 0.0

    def test_increases_with_each_component(self):
        base = se.raw_reach(100, 10, 5, 2)
        assert se.raw_reach(1000, 10, 5, 2) > base
        assert se.raw_reach(100, 100, 5, 2) > base
        assert se.raw_reach(100, 10, 50, 2) > base
        assert se.raw_reach(100, 10, 5, 5) > base

    def test_negative_inputs_clamped_to_zero_contribution(self):
        # Defensive: should never raise on bad upstream data
        assert se.raw_reach(-5, -1, -1, 0) == 0.0


class TestNormalizeMinmaxPerTopic:
    def test_empty_is_empty(self):
        assert se.normalize_minmax_per_topic({}) == {}

    def test_single_value_maps_to_fifty(self):
        k = uuid.uuid4()
        assert se.normalize_minmax_per_topic({k: 42.0}) == {k: 50.0}

    def test_all_equal_values_map_to_fifty(self):
        k1, k2 = uuid.uuid4(), uuid.uuid4()
        result = se.normalize_minmax_per_topic({k1: 7.0, k2: 7.0})
        assert result == {k1: 50.0, k2: 50.0}

    def test_min_maps_to_zero_max_maps_to_hundred(self):
        k_lo, k_hi, k_mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        result = se.normalize_minmax_per_topic({k_lo: 0.0, k_hi: 10.0, k_mid: 5.0})
        assert result[k_lo] == 0.0
        assert result[k_hi] == 100.0
        assert result[k_mid] == pytest.approx(50.0)


class TestRawVelocity:
    def test_no_change_is_zero(self):
        assert se.raw_velocity(10, 10) == pytest.approx(0.0)

    def test_growth_is_positive(self):
        assert se.raw_velocity(20, 10) > 0

    def test_decline_is_negative(self):
        assert se.raw_velocity(5, 10) < 0

    def test_brand_new_claim_no_prior_volume_does_not_divide_by_zero(self):
        result = se.raw_velocity(10, 0)
        assert result == pytest.approx(10 / 1.0)


class TestVelocityZscore:
    def test_zero_z_score_is_fifty(self):
        assert se.velocity_zscore(raw_v=5.0, topic_baseline_mean=5.0, topic_baseline_std=2.0) == pytest.approx(50.0)

    def test_above_baseline_is_above_fifty(self):
        assert se.velocity_zscore(raw_v=10.0, topic_baseline_mean=5.0, topic_baseline_std=2.0) > 50.0

    def test_below_baseline_is_below_fifty(self):
        assert se.velocity_zscore(raw_v=0.0, topic_baseline_mean=5.0, topic_baseline_std=2.0) < 50.0

    def test_large_negative_z_does_not_overflow(self):
        # Regression: naive 100/(1+exp(-z)) overflows math.exp for very negative z.
        assert se.velocity_zscore(raw_v=-1000.0, topic_baseline_mean=0.0, topic_baseline_std=1.0) == pytest.approx(0.0)

    def test_large_positive_z_does_not_overflow(self):
        assert se.velocity_zscore(raw_v=1000.0, topic_baseline_mean=0.0, topic_baseline_std=1.0) == pytest.approx(100.0)

    def test_zero_variance_baseline_does_not_divide_by_zero(self):
        assert se.velocity_zscore(raw_v=100.0, topic_baseline_mean=5.0, topic_baseline_std=0.0) == 50.0

    def test_output_always_in_bounds(self):
        assert 0.0 <= se.velocity_zscore(1000.0, 0.0, 1.0) <= 100.0
        assert 0.0 <= se.velocity_zscore(-1000.0, 0.0, 1.0) <= 100.0


class TestHarmScore:
    def test_matches_documented_formula(self):
        score = se.harm_score(
            public_safety=80, institutional_trust=60, economic=40, policy_disruption=20
        )
        expected = 0.35 * 80 + 0.30 * 60 + 0.20 * 40 + 0.15 * 20
        assert score == pytest.approx(expected)

    def test_all_zero_is_zero(self):
        assert se.harm_score(0, 0, 0, 0) == 0.0

    def test_all_max_is_hundred(self):
        assert se.harm_score(100, 100, 100, 100) == pytest.approx(100.0)


class TestEmotionalIntensity:
    def test_matches_documented_formula(self):
        assert se.emotional_intensity(0.8, 0.4) == pytest.approx(50 * 0.8 + 50 * 0.4)

    def test_both_zero_is_zero(self):
        assert se.emotional_intensity(0.0, 0.0) == 0.0

    def test_both_one_is_hundred(self):
        assert se.emotional_intensity(1.0, 1.0) == pytest.approx(100.0)


class TestClaimScore:
    def test_matches_documented_formula_when_f_present(self):
        score = se.claim_score(r=80, v=60, f=90, h=70, ei=50)
        expected = 0.15 * 80 + 0.15 * 60 + 0.30 * 90 + 0.30 * 70 + 0.10 * 50
        assert score == pytest.approx(expected)

    def test_missing_f_renormalizes_remaining_weights_not_treated_as_zero(self):
        # With F=None, weight should redistribute across R/V/H/EI (sum 0.70) rather
        # than just dropping F's contribution and leaving the sum under-weighted.
        score = se.claim_score(r=100, v=100, f=None, h=100, ei=100)
        assert score == pytest.approx(100.0)

    def test_missing_f_is_not_scored_as_zero(self):
        # If missing F were silently treated as 0, this would be well below 100.
        score_all_max_no_f = se.claim_score(r=100, v=100, f=None, h=100, ei=100)
        score_all_max_low_f = se.claim_score(r=100, v=100, f=0, h=100, ei=100)
        assert score_all_max_no_f > score_all_max_low_f

    def test_output_clamped_to_unit_range(self):
        assert 0.0 <= se.claim_score(1000, 1000, 1000, 1000, 1000) <= 100.0
        assert 0.0 <= se.claim_score(-100, -100, -100, -100, -100) <= 100.0


class TestComputeNPR:
    def test_zero_volume_is_dormant(self):
        npr, is_dormant = se.compute_npr(0, 0)
        assert npr is None
        assert is_dormant is True

    def test_all_opposing_is_one(self):
        npr, is_dormant = se.compute_npr(0, 10)
        assert npr == pytest.approx(1.0)
        assert is_dormant is False

    def test_all_supporting_is_zero(self):
        npr, is_dormant = se.compute_npr(10, 0)
        assert npr == pytest.approx(0.0)
        assert is_dormant is False

    def test_balanced_is_half(self):
        npr, _ = se.compute_npr(10, 10)
        assert npr == pytest.approx(0.5)


class TestDiscountFactor:
    def test_dormant_npr_none_is_no_discount(self):
        assert se.discount_factor(None, total_volume=100) == 1.0

    def test_below_reliability_threshold_is_no_discount(self):
        assert se.discount_factor(1.0, total_volume=se.RELIABILITY_THRESHOLD - 1) == 1.0

    def test_full_pushback_never_fully_zeroes_score(self):
        # gamma=0.5 caps the discount at 50%, even with NPR=1 (total pushback)
        factor = se.discount_factor(1.0, total_volume=se.RELIABILITY_THRESHOLD)
        assert factor == pytest.approx(1.0 - se.GAMMA)
        assert factor > 0.0

    def test_no_pushback_is_no_discount(self):
        assert se.discount_factor(0.0, total_volume=se.RELIABILITY_THRESHOLD) == 1.0


class TestFinalClaimScore:
    def test_multiplies_score_by_discount(self):
        assert se.final_claim_score(80.0, 0.75) == pytest.approx(60.0)

    def test_no_discount_returns_original_score(self):
        assert se.final_claim_score(80.0, 1.0) == pytest.approx(80.0)
