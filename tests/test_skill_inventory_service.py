from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_skill_inventory import UserSkillInventorySnapshot
from src.api.services.skill_inventory_service import (
    MAX_SKILL_DESCRIPTION_BYTES,
    MAX_USER_SKILL_INVENTORY_ITEMS,
    MAX_USER_SKILL_INVENTORY_JSON_BYTES,
    SkillInventoryIdentity,
    SkillInventoryValidationError,
    decode_skill_scan_issues,
    decode_user_skill_inventory,
    load_user_skill_inventory,
    normalize_user_skill_inventory,
    replace_user_skill_inventory,
)


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    UserSandbox.__table__.create(bind=engine)
    UserSkillInventorySnapshot.__table__.create(bind=engine)
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_complete_inventory_snapshot_is_persisted_and_empty_scan_replaces_it():
    engine, session_factory = _session_factory()
    observed_at = datetime(2026, 7, 17, 1, 0, 0)
    try:
        with session_factory() as db:
            db.add(UserSandbox(
                id="binding-1",
                user_id="user-1",
                sandbox_id="sandbox-1",
                active_profile_id="profile-1",
                active_profile_version=2,
            ))
            db.commit()

            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-1", "profile-1", 2),
                skills=[{
                    "name": "my-skill",
                    "display_name": "我的技能",
                    "description": "User uploaded",
                    "sandbox_skill_dir": "/home/user/skills/my-skill",
                    "ignored": "not persisted",
                }],
                observed_at=observed_at,
            ) is True

            snapshot = db.get(UserSkillInventorySnapshot, "user-1")
            assert snapshot is not None
            assert snapshot.revision == 1
            assert decode_user_skill_inventory(snapshot.inventory_json) == [{
                "name": "my-skill",
                "display_name": "我的技能",
                "description": "User uploaded",
                "sandbox_skill_dir": "/home/user/skills/my-skill",
            }]

            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-1", "profile-1", 2),
                skills=[],
                observed_at=observed_at + timedelta(seconds=1),
            ) is True
            db.refresh(snapshot)
            assert decode_user_skill_inventory(snapshot.inventory_json) == []
            assert snapshot.revision == 2
    finally:
        engine.dispose()


def test_older_scan_and_changed_sandbox_cannot_overwrite_snapshot():
    engine, session_factory = _session_factory()
    newer_started_at = datetime(2026, 7, 17, 2, 0, 0)
    try:
        with session_factory() as db:
            binding = UserSandbox(
                id="binding-1",
                user_id="user-1",
                sandbox_id="sandbox-1",
            )
            db.add(binding)
            db.commit()
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-1", None, None),
                skills=[{"name": "newer"}],
                observed_at=newer_started_at,
            ) is True

            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-1", None, None),
                skills=[{"name": "older"}],
                observed_at=newer_started_at - timedelta(seconds=1),
            ) is False
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-1", None, None),
                skills=[{"name": "same-start-time"}],
                observed_at=newer_started_at,
            ) is False

            binding.sandbox_id = "sandbox-2"
            db.commit()
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-1", None, None),
                skills=[{"name": "wrong-generation"}],
                observed_at=newer_started_at + timedelta(seconds=1),
            ) is False

            snapshot = db.get(UserSkillInventorySnapshot, "user-1")
            assert snapshot is not None
            assert decode_user_skill_inventory(snapshot.inventory_json)[0]["name"] == "newer"
    finally:
        engine.dispose()


def test_new_generation_can_replace_future_timestamp_from_old_generation():
    engine, session_factory = _session_factory()
    try:
        with session_factory() as db:
            binding = UserSandbox(
                id="binding-1",
                user_id="user-1",
                sandbox_id="sandbox-old",
                active_profile_id="profile-1",
                active_profile_version=1,
            )
            db.add(binding)
            db.commit()

            future_old_scan = datetime(2026, 7, 17, 4, 0, 0)
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-old", "profile-1", 1),
                skills=[{"name": "old-generation"}],
                observed_at=future_old_scan,
            ) is True

            binding.sandbox_id = "sandbox-new"
            binding.active_profile_version = 2
            db.commit()
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-new", "profile-1", 2),
                skills=[{"name": "new-generation"}],
                observed_at=future_old_scan - timedelta(hours=1),
            ) is True

            snapshot = db.get(UserSkillInventorySnapshot, "user-1")
            assert snapshot is not None
            assert snapshot.sandbox_id == "sandbox-new"
            assert decode_user_skill_inventory(snapshot.inventory_json)[0]["name"] == (
                "new-generation"
            )
    finally:
        engine.dispose()


def test_profile_generation_change_rejects_publish_and_joined_read_hides_old_snapshot():
    engine, session_factory = _session_factory()
    observed_at = datetime(2026, 7, 17, 3, 0, 0)
    try:
        with session_factory() as db:
            binding = UserSandbox(
                id="binding-1",
                user_id="user-1",
                sandbox_id="sandbox-1",
                active_profile_id="profile-1",
                active_profile_version=1,
            )
            db.add(binding)
            db.commit()
            old_identity = SkillInventoryIdentity("sandbox-1", "profile-1", 1)
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=old_identity,
                skills=[{"name": "old-profile-skill"}],
                observed_at=observed_at,
            ) is True

            binding.active_profile_version = 2
            db.commit()
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=old_identity,
                skills=[{"name": "wrong-profile"}],
                observed_at=observed_at + timedelta(seconds=1),
            ) is False

            view = load_user_skill_inventory(db, user_id="user-1")
            assert view.identity == SkillInventoryIdentity("sandbox-1", "profile-1", 2)
            assert view.skills is None
            assert view.discovered_at is None
    finally:
        engine.dispose()


def test_corrupt_inventory_is_a_cache_miss():
    assert decode_user_skill_inventory("not-json") is None
    assert decode_user_skill_inventory('{"name":"not-a-list"}') is None
    assert decode_user_skill_inventory('[{"description":"missing name"}]') is None


def test_corrupt_skill_issues_are_a_cache_miss():
    assert decode_skill_scan_issues("not-json") is None
    assert decode_skill_scan_issues('{"path":"not-a-list"}') is None
    assert decode_skill_scan_issues('["not-an-issue"]') is None
    assert decode_skill_scan_issues("[]") == []


def test_corrupt_skill_issues_invalidate_the_paired_inventory_snapshot():
    engine, session_factory = _session_factory()
    try:
        with session_factory() as db:
            db.add(UserSandbox(
                id="binding-1",
                user_id="user-1",
                sandbox_id="sandbox-1",
            ))
            db.commit()
            assert replace_user_skill_inventory(
                db,
                user_id="user-1",
                identity=SkillInventoryIdentity("sandbox-1", None, None),
                skills=[{"name": "valid-skill"}],
            ) is True

            snapshot = db.get(UserSkillInventorySnapshot, "user-1")
            assert snapshot is not None
            snapshot.issues_json = "not-json"
            db.commit()

            view = load_user_skill_inventory(db, user_id="user-1")
            assert view.skills is None
            assert view.discovered_at is None
            assert view.issues is None
    finally:
        engine.dispose()


def test_inventory_validation_rejects_duplicate_keys_as_one_batch():
    with pytest.raises(SkillInventoryValidationError, match="Duplicate"):
        normalize_user_skill_inventory([
            {"name": " duplicate ", "description": "first"},
            {"name": "duplicate", "description": "second"},
        ])

    with pytest.raises(SkillInventoryValidationError, match="must be a string"):
        normalize_user_skill_inventory([{
            "name": "invalid-description",
            "description": {"unexpected": "mapping"},
        }])


def test_inventory_validation_enforces_item_field_and_total_size_caps():
    with pytest.raises(SkillInventoryValidationError, match="Too many"):
        normalize_user_skill_inventory([
            {"name": f"skill-{index}"}
            for index in range(MAX_USER_SKILL_INVENTORY_ITEMS + 1)
        ])

    with pytest.raises(SkillInventoryValidationError, match="description"):
        normalize_user_skill_inventory([{
            "name": "oversized-description",
            "description": "界" * (MAX_SKILL_DESCRIPTION_BYTES // 3 + 1),
        }])

    individually_valid_but_too_large = [
        {"name": f"skill-{index}", "description": "x" * 6000}
        for index in range(200)
    ]
    with pytest.raises(SkillInventoryValidationError, match="inventory is too large"):
        normalize_user_skill_inventory(individually_valid_but_too_large)


def test_decode_rejects_oversized_raw_snapshot_before_json_parse():
    oversized = "[" + (" " * MAX_USER_SKILL_INVENTORY_JSON_BYTES) + "]"
    with patch(
        "src.api.services.skill_inventory_service.json.loads",
        side_effect=AssertionError("oversized snapshot must not be parsed"),
    ):
        assert decode_user_skill_inventory(oversized) is None
