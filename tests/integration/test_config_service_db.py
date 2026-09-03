"""cis_settings is backend-owned - not in Base.metadata, so these tests create/drop
it by hand rather than relying on the shared schema/truncation fixtures."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app.models.claim import Claim
from app.models.enums import ClaimStatus, ClaimType
from app.models.topic import Topic
from app.services import config_service

pytestmark = pytest.mark.integration


class TestLoadConfigAgainstRealDb:
    async def test_missing_table_falls_back_to_defaults(self, db_session):
        await db_session.execute(text("DROP TABLE IF EXISTS cis_settings"))
        await db_session.commit()

        config = await config_service.load_config(db_session)

        assert config == config_service.DEFAULT_CONFIG

    async def test_a_real_row_overrides_its_default(self, db_session):
        await db_session.execute(text("DROP TABLE IF EXISTS cis_settings"))
        await db_session.execute(
            text("CREATE TABLE cis_settings (key varchar(128) PRIMARY KEY, value text NOT NULL)")
        )
        await db_session.execute(
            text(
                "INSERT INTO cis_settings (key, value) "
                "VALUES ('clustering.claim_attach_threshold', '0.8')"
            )
        )
        await db_session.commit()

        try:
            config = await config_service.load_config(db_session)
            assert config.claim_attach_threshold == 0.8
            # Untouched keys still fall back to their documented default.
            assert (
                config.claim_prefilter_threshold
                == config_service.DEFAULT_CONFIG.claim_prefilter_threshold
            )
        finally:
            await db_session.execute(text("DROP TABLE IF EXISTS cis_settings"))
            await db_session.commit()

    async def test_missing_table_does_not_discard_other_pending_work_on_the_session(
        self, db_session
    ):
        """Regression: load_config used to recover from a missing-table SELECT via a
        bare db.rollback(), which discards the ENTIRE session transaction - including
        a caller's already-flushed-but-uncommitted work (e.g. a just-created Claim),
        not just the failed SELECT. Fixed via a SAVEPOINT (begin_nested)."""
        await db_session.execute(text("DROP TABLE IF EXISTS cis_settings"))
        await db_session.commit()

        topic = Topic(name="Regression Test Topic")
        db_session.add(topic)
        await db_session.flush()
        claim = Claim(
            claim_type=ClaimType.EXISTING,
            claim_statement="A claim flushed but not yet committed.",
            topic_id=topic.id,
            status=ClaimStatus.UNREVIEWED,
            first_caught_at=datetime.now(UTC),
        )
        db_session.add(claim)
        await db_session.flush()  # pending in this transaction, not yet committed

        config = await config_service.load_config(db_session)
        assert config == config_service.DEFAULT_CONFIG

        # The claim flushed before load_config() must still be visible on this same
        # session/transaction - a bare rollback() would have wiped it out here.
        found = (await db_session.execute(select(Claim).where(Claim.id == claim.id))).scalar_one_or_none()
        assert found is not None
