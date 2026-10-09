"""User bookkeeping, raw-message capture and Telegram update idempotency."""
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedUpdate, RawMessage, User

log = logging.getLogger(__name__)


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None = None) -> User:
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if user is not None:
        if username and user.username != username:
            user.username = username[:64]
        return user

    try:
        async with session.begin_nested():
            user = User(telegram_id=telegram_id, username=(username or None) and username[:64])
            session.add(user)
    except IntegrityError:
        # Another worker created it between our SELECT and INSERT.
        user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
    return user


async def already_processed(session: AsyncSession, update_id: int | None) -> bool:
    if not update_id:
        return False
    return (await session.get(ProcessedUpdate, update_id)) is not None


async def mark_processed(session: AsyncSession, update_id: int | None) -> None:
    if not update_id:
        return
    if await session.get(ProcessedUpdate, update_id) is None:
        session.add(ProcessedUpdate(update_id=update_id))


async def store_raw(session: AsyncSession, user: User, text: str, update_id: int | None) -> RawMessage:
    msg = RawMessage(user_id=user.id, text=text, telegram_update_id=update_id)
    session.add(msg)
    await session.flush()
    return msg
