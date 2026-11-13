"""Close organizations whose 30-day deletion grace period has ended.

Run daily (cron / scheduler):  make purge-organizations
"""

import asyncio

from app.core.db import sessionmanager
from app.core.logger import get_logger
from app.services.tenancy import purge_expired_organizations

logger = get_logger()


async def main() -> None:
    sessionmanager.init_db()
    assert sessionmanager.session_factory is not None
    try:
        async with sessionmanager.session_factory() as session:
            purged = await purge_expired_organizations(session)
        logger.info(
            "Purged organizations past their deletion grace period",
            extra={"count": len(purged), "organization_ids": [str(i) for i in purged]},
        )
    finally:
        await sessionmanager.close()


if __name__ == "__main__":
    asyncio.run(main())
