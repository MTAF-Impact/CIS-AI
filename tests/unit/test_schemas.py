from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.enums import ContentSource, MoralFoundation
from app.schemas.analysis import ContentAnalysisSchema
from app.schemas.content import ContentItemBatchCreate, ContentItemCreate
from app.schemas.coordination import CIBCheckPost, CIBCheckRequest


class TestContentAnalysisSchema:
    def test_valid_payload(self):
        schema = ContentAnalysisSchema(
            outrage_score=0.5,
            moral_foundation=MoralFoundation.FAIRNESS,
            extracted_claim="claim",
            underlying_grievance="grievance",
        )
        assert schema.outrage_score == 0.5

    @pytest.mark.parametrize("outrage_score", [-0.1, 1.1])
    def test_outrage_score_out_of_bounds_rejected(self, outrage_score):
        with pytest.raises(ValidationError):
            ContentAnalysisSchema(
                outrage_score=outrage_score,
                moral_foundation=MoralFoundation.NEUTRAL,
                extracted_claim="claim",
                underlying_grievance="grievance",
            )

    def test_unknown_moral_foundation_value_rejected(self):
        with pytest.raises(ValidationError):
            ContentAnalysisSchema(
                outrage_score=0.5,
                moral_foundation="not_a_real_moral_foundation",
                extracted_claim="claim",
                underlying_grievance="grievance",
            )


class TestContentItemCreate:
    def test_requires_non_empty_text(self):
        with pytest.raises(ValidationError):
            ContentItemCreate(text="", source=ContentSource.SOCIAL)

    def test_source_defaults_to_other(self):
        item = ContentItemCreate(text="hello")
        assert item.source == ContentSource.OTHER

    def test_optional_metrics_default_to_none(self):
        item = ContentItemCreate(text="hello")
        assert item.impressions is None
        assert item.positive_reaction_count is None
        assert item.negative_reaction_count is None


class TestContentItemBatchCreate:
    def test_requires_at_least_one_item(self):
        with pytest.raises(ValidationError):
            ContentItemBatchCreate(items=[])

    def test_accepts_one_item(self):
        batch = ContentItemBatchCreate(items=[ContentItemCreate(text="hello")])
        assert len(batch.items) == 1


class TestCIBCheckRequest:
    def _post(self, id_: str) -> CIBCheckPost:
        return CIBCheckPost(
            id=id_, text="hello", author_id="u1", created_at=datetime.now(UTC)
        )

    def test_requires_at_least_two_posts(self):
        with pytest.raises(ValidationError):
            CIBCheckRequest(posts=[self._post("1")])

    def test_accepts_two_posts(self):
        request = CIBCheckRequest(posts=[self._post("1"), self._post("2")])
        assert len(request.posts) == 2
