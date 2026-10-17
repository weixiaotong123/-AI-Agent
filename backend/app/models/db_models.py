from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import Base


class TripRecord(Base):
    """当前版本使用的最小行程表。"""

    __tablename__ = "trip_records"

    # 数据库内部主键
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 业务侧使用的 itinerary 标识
    trip_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    destination: Mapped[str] = mapped_column(String(100))
    summary: Mapped[str] = mapped_column(Text)
    # 完整 itinerary 的 JSON 字符串
    itinerary_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
class GlobalConstraintRecord(Base):
    """全局硬约束：过敏、忌口等安全相关规则，优先级最高，不参与覆盖仲裁。"""

    __tablename__ = "global_constraint_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    constraint_type: Mapped[str] = mapped_column(String(50))
    value: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GlobalPreferenceRecord(Base):
    """全局长期偏好：口味、节奏等软性偏好，可被当前指令覆盖，会覆盖目的地偏好。"""

    __tablename__ = "global_preference_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    preference_key: Mapped[str] = mapped_column(String(50))
    preference_value: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class DestinationPreferenceRecord(Base):
    """按目的地沉淀的偏好，优先级最低（仅次于默认策略），会被全局偏好覆盖。"""

    __tablename__ = "destination_preference_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    destination: Mapped[str] = mapped_column(String(100), index=True)
    preference_key: Mapped[str] = mapped_column(String(50))
    preference_value: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )