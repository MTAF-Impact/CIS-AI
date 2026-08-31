import pytest

from app.models.official_source import OfficialSource
from app.services import falseness_service

pytestmark = pytest.mark.integration


class TestComputeFalsenessScore:
    async def test_empty_corpus_returns_none(self, db_session, real_embedder):
        embedding = real_embedder.embed("The ERP congestion charge is a hidden tax.")
        score = await falseness_service.compute_falseness_score(db_session, embedding)
        assert score is None

    async def test_confident_match_returns_a_score(self, db_session, real_embedder):
        source_text = (
            "Official statement: the ERP road pricing pilot does not include any "
            "hidden tax or additional congestion charge beyond the published toll rate."
        )
        db_session.add(
            OfficialSource(
                title="City ERP Fact Sheet",
                content=source_text,
                embedding=real_embedder.embed(source_text),
            )
        )
        await db_session.commit()

        claim_embedding = real_embedder.embed(
            "The ERP congestion charge is secretly a hidden tax on commuters."
        )
        score = await falseness_service.compute_falseness_score(db_session, claim_embedding)

        assert score is not None
        assert 0.0 <= score <= 100.0

    async def test_no_confident_match_returns_none(self, db_session, real_embedder):
        unrelated_text = "The city library will extend its weekend opening hours starting next month."
        db_session.add(
            OfficialSource(
                title="Library Hours Notice",
                content=unrelated_text,
                embedding=real_embedder.embed(unrelated_text),
            )
        )
        await db_session.commit()

        claim_embedding = real_embedder.embed(
            "The ERP congestion charge is secretly a hidden tax on commuters."
        )
        score = await falseness_service.compute_falseness_score(
            db_session, claim_embedding, threshold=0.55
        )

        assert score is None
