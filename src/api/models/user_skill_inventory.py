"""Persisted snapshots of user-installed Skill metadata."""

from sqlalchemy import Column, DateTime, Integer, String, Text

from src.api.models.database import Base
from src.api.utils.timezone import now_naive


class UserSkillInventorySnapshot(Base):
    """Last complete sandbox Skill scan for one user.

    ``inventory_json`` stores metadata only; Skill content remains in the
    sandbox and enable/disable state remains in ``user_skill_configs``.
    """

    __tablename__ = "user_skill_inventory_snapshots"

    user_id = Column(String(100), primary_key=True)
    sandbox_id = Column(String(100), nullable=False)
    active_profile_id = Column(String(36), nullable=True)
    active_profile_version = Column(Integer, nullable=True)
    inventory_json = Column(Text, nullable=False)
    issues_json = Column(Text, nullable=False, default="[]")
    revision = Column(Integer, nullable=False, default=1)
    # This is the scan start time. A later-started scan may publish first and
    # must not subsequently be overwritten by an older, slower scan.
    discovered_at = Column(DateTime, nullable=False, default=now_naive)
    updated_at = Column(DateTime, nullable=False, default=now_naive, onupdate=now_naive)
