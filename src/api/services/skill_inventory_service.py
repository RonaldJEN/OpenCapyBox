"""DB-backed metadata snapshots for user-installed Skills."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session as DBSession

from src.agent.schema.skill_key import normalize_skill_key
from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_skill_inventory import UserSkillInventorySnapshot
from src.api.utils.timezone import now_naive


MAX_USER_SKILL_INVENTORY_ITEMS = 256
MAX_SKILL_DISPLAY_NAME_BYTES = 1024
MAX_SKILL_DESCRIPTION_BYTES = 8192
MAX_SKILL_SANDBOX_DIR_BYTES = 1024
MAX_USER_SKILL_INVENTORY_JSON_BYTES = 1024 * 1024
MAX_SKILL_SCAN_ISSUES = 256
MAX_SKILL_ISSUES_JSON_BYTES = 256 * 1024


def _bounded_issue_text(value: object, *, default: str, max_bytes: int) -> str:
    """Best-effort bound untrusted diagnostics without failing a valid scan."""
    text = value if isinstance(value, str) else default
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class SkillInventoryValidationError(ValueError):
    """A complete user Skill scan is invalid and must not be published."""


def _bounded_text(
    value: object,
    *,
    default: str,
    max_bytes: int,
    error_message: str,
    type_error_message: str,
) -> str:
    if value is None:
        text = default
    elif isinstance(value, str):
        text = value
    else:
        raise SkillInventoryValidationError(type_error_message)
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SkillInventoryValidationError(type_error_message) from exc
    if encoded_size > max_bytes:
        raise SkillInventoryValidationError(error_message)
    return text


def _encode_inventory_json(skills: list[dict[str, str]]) -> str:
    value = json.dumps(
        skills,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SkillInventoryValidationError(
            "User Skill inventory contains invalid text"
        ) from exc
    if encoded_size > MAX_USER_SKILL_INVENTORY_JSON_BYTES:
        raise SkillInventoryValidationError("User Skill inventory is too large")
    return value


def normalize_skill_scan_issues(
    issues: Iterable[Mapping[str, object]],
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw in issues:
        if len(normalized) >= MAX_SKILL_SCAN_ISSUES:
            break
        if not isinstance(raw, Mapping):
            continue
        item = {
            "path": _bounded_issue_text(
                raw.get("path"),
                default="",
                max_bytes=MAX_SKILL_SANDBOX_DIR_BYTES,
            ),
            "field": _bounded_issue_text(
                raw.get("field"),
                default="frontmatter",
                max_bytes=256,
            ),
            "message": _bounded_issue_text(
                raw.get("message"),
                default="Skill 配置无效",
                max_bytes=4096,
            ),
            "suggestion": _bounded_issue_text(
                raw.get("suggestion"),
                default="请检查 SKILL.md。",
                max_bytes=4096,
            ),
        }
        candidate = [*normalized, item]
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_SKILL_ISSUES_JSON_BYTES:
            break
        normalized.append(item)
    return normalized


def _encode_issues_json(issues: Iterable[Mapping[str, object]]) -> str:
    return json.dumps(
        normalize_skill_scan_issues(issues),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_skill_scan_issues(value: object) -> list[dict[str, str]] | None:
    if not isinstance(value, str):
        return None
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if encoded_size > MAX_SKILL_ISSUES_JSON_BYTES:
        return None
    try:
        raw = json.loads(value)
        if not isinstance(raw, list):
            return None
        normalized = normalize_skill_scan_issues(raw)
        # 写入端只会发布规范化后的完整列表；读取时发生任何截断、默认值
        # 补齐或非法项丢弃，都说明配对的诊断快照已损坏。
        return normalized if normalized == raw else None
    except (TypeError, ValueError, SkillInventoryValidationError):
        return None


@dataclass(frozen=True)
class SkillInventoryIdentity:
    """Immutable sandbox/Profile generation that produced one complete scan."""

    sandbox_id: str
    active_profile_id: str | None
    active_profile_version: int | None


@dataclass(frozen=True)
class UserSkillInventoryView:
    """One consistent DB view of the current binding and matching snapshot."""

    identity: SkillInventoryIdentity | None
    skills: list[dict[str, str]] | None
    discovered_at: datetime | None
    issues: list[dict[str, str]] | None = None


def inventory_view_is_current_winner(
    view: UserSkillInventoryView,
    *,
    identity: SkillInventoryIdentity,
    observed_at: datetime,
) -> bool:
    """Return whether ``view`` safely won against this exact scan attempt."""

    return (
        view.identity == identity
        and view.skills is not None
        and view.discovered_at is not None
        and view.discovered_at >= observed_at
    )


def identity_from_binding(binding: UserSandbox | None) -> SkillInventoryIdentity | None:
    if binding is None or not isinstance(binding.sandbox_id, str) or not binding.sandbox_id:
        return None
    return SkillInventoryIdentity(
        sandbox_id=binding.sandbox_id,
        active_profile_id=binding.active_profile_id,
        active_profile_version=binding.active_profile_version,
    )


def cached_sandbox_identity(
    sandbox_service,
    user_id: str,
) -> SkillInventoryIdentity | None:
    """Capture the in-process sandbox generation without remote I/O."""

    sandbox_id = sandbox_service.get_sandbox_id(user_id)
    if not isinstance(sandbox_id, str) or not sandbox_id:
        return None
    profile_id, profile_version = sandbox_service.get_cached_profile_fingerprint(user_id)
    return SkillInventoryIdentity(
        sandbox_id=sandbox_id,
        active_profile_id=profile_id,
        active_profile_version=profile_version,
    )


def normalize_user_skill_inventory(
    skills: Iterable[Mapping[str, object]],
) -> list[dict[str, str]]:
    """Validate and canonicalize the usable part of one complete scan.

    The sandbox scanner isolates unreadable or malformed files first and
    publishes their diagnostics alongside this list. Anything reaching this
    boundary must already be valid and unique; corruption here rejects the
    publication because it indicates a scanner/programming error.
    """

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in skills:
        if len(normalized) >= MAX_USER_SKILL_INVENTORY_ITEMS:
            raise SkillInventoryValidationError("Too many user Skills")
        if not isinstance(raw, Mapping):
            raise SkillInventoryValidationError("User Skill metadata must be an object")
        name_value = raw.get("name")
        if not isinstance(name_value, str):
            raise SkillInventoryValidationError("User Skill key is missing")
        try:
            name = normalize_skill_key(name_value)
        except ValueError as exc:
            raise SkillInventoryValidationError(str(exc)) from exc
        if name in seen:
            raise SkillInventoryValidationError("Duplicate user Skill key")
        seen.add(name)

        display_name_value = raw.get("display_name")
        if isinstance(display_name_value, str):
            display_name_value = display_name_value.strip() or name
        display_name = _bounded_text(
            display_name_value,
            default=name,
            max_bytes=MAX_SKILL_DISPLAY_NAME_BYTES,
            error_message="User Skill display name is too large",
            type_error_message="User Skill display name must be a string",
        )
        description = _bounded_text(
            raw.get("description"),
            default="",
            max_bytes=MAX_SKILL_DESCRIPTION_BYTES,
            error_message="User Skill description is too large",
            type_error_message="User Skill description must be a string",
        )
        sandbox_skill_dir = _bounded_text(
            raw.get("sandbox_skill_dir"),
            default="",
            max_bytes=MAX_SKILL_SANDBOX_DIR_BYTES,
            error_message="User Skill path is too large",
            type_error_message="User Skill path must be a string",
        )

        normalized.append({
            "name": name,
            "display_name": display_name,
            "description": description,
            "sandbox_skill_dir": sandbox_skill_dir,
        })
    _encode_inventory_json(normalized)
    return normalized


def decode_user_skill_inventory(value: object) -> list[dict[str, str]] | None:
    """Decode a complete snapshot; corruption is treated as a cache miss."""

    if not isinstance(value, str):
        return None
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if encoded_size > MAX_USER_SKILL_INVENTORY_JSON_BYTES:
        return None
    try:
        raw = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, list):
        return None
    if any(not isinstance(item, dict) for item in raw):
        return None
    try:
        return normalize_user_skill_inventory(raw)
    except SkillInventoryValidationError:
        return None


def snapshot_matches_binding(
    snapshot: UserSkillInventorySnapshot | None,
    binding: UserSandbox | None,
) -> bool:
    if snapshot is None or binding is None:
        return False
    return (
        snapshot.sandbox_id == binding.sandbox_id
        and snapshot.active_profile_id == binding.active_profile_id
        and snapshot.active_profile_version == binding.active_profile_version
    )


def load_user_skill_inventory(
    db: DBSession,
    *,
    user_id: str,
) -> UserSkillInventoryView:
    """Read binding and snapshot with one SQL statement.

    A single joined read avoids accepting a snapshot for binding A after a
    concurrent transaction has already switched the user to binding B.
    """

    row = (
        db.query(UserSandbox, UserSkillInventorySnapshot)
        .outerjoin(
            UserSkillInventorySnapshot,
            UserSkillInventorySnapshot.user_id == UserSandbox.user_id,
        )
        .filter(UserSandbox.user_id == user_id)
        .first()
    )
    if row is None:
        return UserSkillInventoryView(None, None, None, None)

    binding, snapshot = row
    identity = identity_from_binding(binding)
    if identity is None or not snapshot_matches_binding(snapshot, binding):
        return UserSkillInventoryView(identity, None, None, None)

    skills = decode_user_skill_inventory(snapshot.inventory_json)
    if skills is None:
        return UserSkillInventoryView(identity, None, None, None)
    issues = decode_skill_scan_issues(getattr(snapshot, "issues_json", None))
    if issues is None:
        return UserSkillInventoryView(identity, None, None, None)
    return UserSkillInventoryView(identity, skills, snapshot.discovered_at, issues)


def replace_user_skill_inventory(
    db: DBSession,
    *,
    user_id: str,
    identity: SkillInventoryIdentity,
    skills: Iterable[Mapping[str, object]],
    issues: Iterable[Mapping[str, object]] = (),
    observed_at: datetime | None = None,
) -> bool:
    """Atomically publish one complete scan if its sandbox generation is current.

    The binding row is locked only during this short DB transaction, never
    while remote sandbox I/O is in flight. Older concurrent scans cannot
    overwrite a scan that started later.
    """

    observed_at = observed_at or now_naive()
    normalized = normalize_user_skill_inventory(skills)
    inventory_json = _encode_inventory_json(normalized)
    issues_json = _encode_issues_json(issues)

    try:
        binding = (
            db.query(UserSandbox)
            .filter(UserSandbox.user_id == user_id)
            .with_for_update()
            .first()
        )
        if identity_from_binding(binding) != identity:
            db.rollback()
            return False

        snapshot = (
            db.query(UserSkillInventorySnapshot)
            .filter(UserSkillInventorySnapshot.user_id == user_id)
            .with_for_update()
            .first()
        )
        snapshot_identity = (
            SkillInventoryIdentity(
                snapshot.sandbox_id,
                snapshot.active_profile_id,
                snapshot.active_profile_version,
            )
            if snapshot is not None
            else None
        )
        if (
            snapshot_identity == identity
            and snapshot.discovered_at is not None
            and snapshot.discovered_at >= observed_at
        ):
            db.rollback()
            return False

        if snapshot is None:
            snapshot = UserSkillInventorySnapshot(
                user_id=user_id,
                sandbox_id=identity.sandbox_id,
                active_profile_id=identity.active_profile_id,
                active_profile_version=identity.active_profile_version,
                inventory_json=inventory_json,
                issues_json=issues_json,
                revision=1,
                discovered_at=observed_at,
            )
            db.add(snapshot)
        else:
            snapshot.sandbox_id = identity.sandbox_id
            snapshot.active_profile_id = identity.active_profile_id
            snapshot.active_profile_version = identity.active_profile_version
            snapshot.inventory_json = inventory_json
            snapshot.issues_json = issues_json
            snapshot.revision = int(snapshot.revision or 0) + 1
            snapshot.discovered_at = observed_at
            snapshot.updated_at = now_naive()
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def persist_user_skill_inventory(
    db_session_factory,
    *,
    user_id: str,
    identity: SkillInventoryIdentity | None,
    skills: Iterable[Mapping[str, object]],
    issues: Iterable[Mapping[str, object]] = (),
    observed_at: datetime | None = None,
) -> bool:
    """Open a short-lived DB session and publish a successful scan."""

    if identity is None:
        return False
    db = db_session_factory()
    try:
        return replace_user_skill_inventory(
            db,
            user_id=user_id,
            identity=identity,
            skills=skills,
            issues=issues,
            observed_at=observed_at,
        )
    finally:
        db.close()
