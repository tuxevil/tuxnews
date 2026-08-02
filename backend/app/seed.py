import asyncio

from sqlalchemy import select

from app.audit.service import record_audit
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionFactory


async def seed_initial_admin() -> None:
    settings = get_settings()
    async with SessionFactory() as session:
        existing = await session.scalar(select(User).where(User.email == settings.initial_admin_email.lower()))
        if existing is None:
            any_user = await session.scalar(select(User.id).limit(1))
            if any_user is not None:
                return
            user = User(
                email=settings.initial_admin_email.lower(),
                password_hash=hash_password(settings.initial_admin_password),
                role="admin",
            )
            session.add(user)
            await session.flush()
            record_audit(
                session,
                user_id=user.id,
                action="user.seeded",
                resource_type="user",
                resource_id=str(user.id),
                outcome="success",
                actor_type="system",
                actor_id="seed_initial_admin",
            )
            await session.commit()


if __name__ == "__main__":
    asyncio.run(seed_initial_admin())
