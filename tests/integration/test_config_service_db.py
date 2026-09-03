"""cis_settings is backend-owned - not in Base.metadata, so these tests create/drop
it by hand rather than relying on the shared schema/truncation fixtures."""

import pytest
from sqlalchemy import text

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
