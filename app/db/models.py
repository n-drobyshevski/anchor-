"""SQLAlchemy 2.0 declarative models.

1a defines only TelegramUpdate — the inbound queue and dedup table. The
rest of the Phase 1 schema (message, user_state, state_change,
persona_version, spend_ledger) lands in 1b, per plan section 17. Keeping
1a's migration to the one table it actually uses keeps the 1a deploy
independently revertible.
"""

from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TelegramUpdate(Base):
    __tablename__ = "telegram_update"

    # Telegram's own update_id, provided explicitly on insert — not a
    # generated identity column.
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_telegram_update_status_update_id", "status", "update_id"),)
