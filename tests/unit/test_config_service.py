import pytest

from app.services import config_service


class TestBuildDefaults:
    def test_no_rows_reproduces_documented_defaults(self):
        assert config_service._build({}) == config_service.DEFAULT_CONFIG

    def test_score_weights_sum_to_one_by_default(self):
        w = config_service.DEFAULT_CONFIG.score_weights
        total = w.reach + w.velocity + w.falseness + w.harm + w.emotional_intensity
        assert total == pytest.approx(1.0)

    def test_velocity_epsilon_defaults_to_one_not_the_doc_suggested_value(self):
        # AI_DYNAMIC_PARAMETER.md suggests 0.0001; we deliberately keep the existing
        # low-volume damping (1.0) instead - see DEFAULTS' comment for why.
        assert config_service.DEFAULT_CONFIG.velocity_epsilon == 1.0

    def test_velocity_and_npr_windows_default_to_todays_single_window(self):
        # Both default to 24h (today's ROLLING_WINDOW_HOURS), not the doc's 6/36 -
        # diverging is an intentional follow-up, not implicit.
        assert config_service.DEFAULT_CONFIG.velocity_interval_hours == 24.0
        assert config_service.DEFAULT_CONFIG.npr_window_hours == 24.0


class TestBuildOverrides:
    def test_a_present_row_overrides_its_default(self):
        config = config_service._build({"clustering.claim_attach_threshold": "0.8"})
        assert config.claim_attach_threshold == 0.8

    def test_unrelated_keys_still_fall_back_when_only_one_row_is_present(self):
        config = config_service._build({"clustering.claim_attach_threshold": "0.8"})
        assert config.claim_prefilter_threshold == config_service.DEFAULT_CONFIG.claim_prefilter_threshold

    def test_policy_disruption_weight_is_clamped_to_the_ceiling(self):
        config = config_service._build({"scoring.harm_weight_policy_disruption": "0.9"})
        assert config.harm_weights.policy_disruption == config_service.POLICY_DISRUPTION_WEIGHT_CEILING

    def test_policy_disruption_weight_below_ceiling_passes_through_unclamped(self):
        config = config_service._build({"scoring.harm_weight_policy_disruption": "0.1"})
        assert config.harm_weights.policy_disruption == 0.1

    def test_mismatched_score_weights_sum_is_logged_not_raised(self, caplog):
        with caplog.at_level("WARNING"):
            config = config_service._build({"scoring.weight_reach": "0.9"})
        assert config.score_weights.reach == 0.9  # used anyway, never rejected
        assert "not 1.00" in caplog.text

    def test_integer_valued_key_casts_via_float_first(self):
        # value_type=number covers both int and float per the doc - "25" and "25.0"
        # must both parse for an integer-typed field like the reliability minimum.
        assert config_service._build({"scoring.npr_reliability_minimum_posts": "25.0"}).npr_reliability_minimum_posts == 25
