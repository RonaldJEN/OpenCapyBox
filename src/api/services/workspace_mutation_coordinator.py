"""Durable ownership for workspace filesystem mutations.

Claims and their prepared mutation are committed together before Sandbox I/O,
then renewed from independent short database sessions.  They serialize only
overlapping file/path/tree scopes; unrelated workspace entries remain
concurrent.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterator, Callable, Iterable

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.auth_user import AuthUser
from src.api.models.workspace import UserWorkspace, WorkspaceClaim, WorkspaceMutation
from src.api.utils.timezone import now_naive


_VALID_SCOPE_KINDS = {"file", "path", "tree", "workspace"}


class WorkspaceClaimConflict(RuntimeError):
    def __init__(self, scope_keys: Iterable[str]) -> None:
        self.scope_keys = tuple(sorted(set(scope_keys)))
        super().__init__("工作区目标正在被其他操作修改，请稍后重试")


class WorkspaceClaimLost(RuntimeError):
    def __init__(self, claim_id: str) -> None:
        self.claim_id = claim_id
        super().__init__("工作区修改所有权已失效")


class WorkspaceDraining(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceClaimSpec:
    scope_kind: str
    scope_key: str
    entry_id: str | None = None
    conflict_scope_keys: tuple[str, ...] = ()
    conflict_entry_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceClaimLease:
    claim_id: str
    user_id: str
    scope_kind: str
    scope_key: str
    owner_token: str
    generation: int
    lease_expires_at: Any


@dataclass(frozen=True)
class WorkspaceClaimTakeover:
    previous: tuple[WorkspaceClaimLease, ...]
    current: tuple[WorkspaceClaimLease, ...]


def file_scope(entry_id: str) -> str:
    return f"file:{entry_id}"


def tree_scope(entry_id: str) -> str:
    return f"tree:{entry_id}"


def workspace_scope(user_id: str) -> str:
    return f"workspace:{user_id}"


def path_scope(parent_id: str | None, name: str) -> str:
    normalized = f"{parent_id or '<root>'}\0{name}".encode("utf-8")
    return f"path:{hashlib.sha256(normalized).hexdigest()}"


class WorkspaceMutationCoordinator:
    """Acquire, renew, assert, and release durable workspace claims."""

    def __init__(self, db: DBSession) -> None:
        self.db = db
        self.settings = get_settings()

    def acquire_claims(
        self,
        *,
        user_id: str,
        operation: str,
        specs: Iterable[WorkspaceClaimSpec],
        owner_token: str | None = None,
        mutation_id: str | None = None,
        lease_seconds: int | None = None,
        commit: bool = True,
    ) -> list[WorkspaceClaimLease]:
        normalized_specs = self._normalize_specs(specs)
        if not normalized_specs:
            raise ValueError("至少需要一个工作区 claim scope")
        self._lock_user(user_id)
        workspace = (
            self.db.query(UserWorkspace)
            .filter(UserWorkspace.user_id == user_id)
            .with_for_update()
            .one_or_none()
        )
        if workspace is None:
            workspace = UserWorkspace(
                user_id=user_id,
                root_path="/home/user/workdir",
                quota_bytes=int(self.settings.workspace_quota_bytes),
                history_quota_bytes=int(self.settings.workspace_history_quota_bytes),
                status="active",
            )
            self.db.add(workspace)
            self.db.flush()
        if workspace.status != "active":
            raise WorkspaceDraining("工作区正在切换运行环境，暂不接受新修改")

        current_time = now_naive()
        self.db.query(WorkspaceClaim).filter(
            WorkspaceClaim.user_id == user_id,
            WorkspaceClaim.state == "active",
            WorkspaceClaim.mutation_id.is_(None),
            WorkspaceClaim.lease_expires_at <= current_time,
        ).update(
            {"state": "fenced", "released_at": current_time},
            synchronize_session="fetch",
        )
        requested_keys = {spec.scope_key for spec in normalized_specs}
        conflict_keys = requested_keys | {
            key
            for spec in normalized_specs
            for key in spec.conflict_scope_keys
        }
        conflict_entry_ids = {
            entry_id
            for spec in normalized_specs
            for entry_id in spec.conflict_entry_ids
            if entry_id
        }
        overlap_filter = WorkspaceClaim.scope_key.in_(tuple(conflict_keys))
        if conflict_entry_ids:
            overlap_filter = or_(
                overlap_filter,
                WorkspaceClaim.entry_id.in_(tuple(conflict_entry_ids)),
            )
        conflicting_rows = (
            self.db.query(WorkspaceClaim)
            .filter(
                WorkspaceClaim.user_id == user_id,
                overlap_filter,
                WorkspaceClaim.state == "active",
            )
            .with_for_update()
            .all()
        )
        if conflicting_rows:
            self.db.rollback()
            raise WorkspaceClaimConflict(claim.scope_key for claim in conflicting_rows)

        token = owner_token or uuid.uuid4().hex
        duration = max(
            int(lease_seconds or self.settings.workspace_mutation_lease_seconds),
            10,
        )
        leases: list[WorkspaceClaimLease] = []
        for spec in normalized_specs:
            latest_generation = (
                self.db.query(func.max(WorkspaceClaim.generation))
                .filter(
                    WorkspaceClaim.user_id == user_id,
                    WorkspaceClaim.scope_key == spec.scope_key,
                )
                .scalar()
            )
            generation = int(latest_generation or 0) + 1
            claim_id = str(uuid.uuid4())
            expires_at = current_time + timedelta(seconds=duration)
            self.db.add(WorkspaceClaim(
                claim_id=claim_id,
                user_id=user_id,
                scope_kind=spec.scope_kind,
                scope_key=spec.scope_key,
                entry_id=spec.entry_id,
                mutation_id=mutation_id,
                operation=operation,
                owner_token=token,
                generation=generation,
                state="active",
                lease_expires_at=expires_at,
                heartbeat_at=current_time,
            ))
            leases.append(WorkspaceClaimLease(
                claim_id=claim_id,
                user_id=user_id,
                scope_kind=spec.scope_kind,
                scope_key=spec.scope_key,
                owner_token=token,
                generation=generation,
                lease_expires_at=expires_at,
            ))
        if commit:
            try:
                self.db.commit()
            except IntegrityError as exc:
                self.db.rollback()
                raise WorkspaceClaimConflict(requested_keys) from exc
        return leases

    def renew_claims(
        self,
        leases: Iterable[WorkspaceClaimLease],
        *,
        lease_seconds: int | None = None,
    ) -> None:
        current_time = now_naive()
        duration = max(
            int(lease_seconds or self.settings.workspace_mutation_lease_seconds),
            10,
        )
        for lease in leases:
            updated = (
                self.db.query(WorkspaceClaim)
                .filter(
                    *self._owner_filters(lease),
                    WorkspaceClaim.state == "active",
                )
                .update(
                    {
                        "heartbeat_at": current_time,
                        "lease_expires_at": current_time + timedelta(seconds=duration),
                    },
                    synchronize_session="fetch",
                )
            )
            if updated != 1:
                self.db.rollback()
                raise WorkspaceClaimLost(lease.claim_id)
        self.db.commit()

    def assert_claims(self, leases: Iterable[WorkspaceClaimLease]) -> None:
        for lease in leases:
            row = (
                self.db.query(WorkspaceClaim.claim_id)
                .filter(
                    *self._owner_filters(lease),
                    WorkspaceClaim.state == "active",
                )
                .first()
            )
            if row is None:
                raise WorkspaceClaimLost(lease.claim_id)

    def release_claims(
        self,
        leases: Iterable[WorkspaceClaimLease],
        *,
        final_state: str = "released",
        commit: bool = True,
    ) -> None:
        if final_state not in {"released", "fenced"}:
            raise ValueError("claim final_state 必须为 released/fenced")
        released_at = now_naive()
        for lease in leases:
            self.db.query(WorkspaceClaim).filter(
                *self._owner_filters(lease),
                WorkspaceClaim.state == "active",
            ).update(
                {"state": final_state, "released_at": released_at},
                synchronize_session="fetch",
            )
        if commit:
            self.db.commit()

    def takeover_expired_mutation_claims(
        self,
        *,
        user_id: str,
        mutation_id: str,
        lease_seconds: int | None = None,
    ) -> WorkspaceClaimTakeover:
        """Move DB ownership while retaining the previous filesystem identity."""
        self._lock_user(user_id)
        self.db.query(UserWorkspace.user_id).filter(
            UserWorkspace.user_id == user_id,
        ).with_for_update().one()
        claims = (
            self.db.query(WorkspaceClaim)
            .filter(
                WorkspaceClaim.user_id == user_id,
                WorkspaceClaim.mutation_id == mutation_id,
                WorkspaceClaim.state == "active",
            )
            .order_by(WorkspaceClaim.scope_key.asc())
            .with_for_update()
            .all()
        )
        if not claims:
            self.db.rollback()
            return WorkspaceClaimTakeover((), ())
        current_time = now_naive()
        if any(claim.lease_expires_at > current_time for claim in claims):
            self.db.rollback()
            raise WorkspaceClaimConflict(claim.scope_key for claim in claims)
        mutation = self.db.query(WorkspaceMutation).filter(
            WorkspaceMutation.mutation_id == mutation_id,
            WorkspaceMutation.user_id == user_id,
            WorkspaceMutation.state == "prepared",
        ).with_for_update().one_or_none()
        if mutation is None:
            self.db.rollback()
            raise WorkspaceClaimLost(claims[0].claim_id)
        previous = tuple(
            WorkspaceClaimLease(
                claim_id=claim.claim_id,
                user_id=user_id,
                scope_kind=claim.scope_kind,
                scope_key=claim.scope_key,
                owner_token=claim.owner_token,
                generation=int(claim.generation),
                lease_expires_at=claim.lease_expires_at,
            )
            for claim in claims
        )
        token = uuid.uuid4().hex
        duration = max(
            int(lease_seconds or self.settings.workspace_mutation_lease_seconds),
            10,
        )
        expires_at = current_time + timedelta(seconds=duration)
        leases: list[WorkspaceClaimLease] = []
        for claim in claims:
            claim.owner_token = token
            claim.generation = int(claim.generation or 0) + 1
            claim.heartbeat_at = current_time
            claim.lease_expires_at = expires_at
            leases.append(WorkspaceClaimLease(
                claim_id=claim.claim_id,
                user_id=user_id,
                scope_kind=claim.scope_kind,
                scope_key=claim.scope_key,
                owner_token=token,
                generation=int(claim.generation),
                lease_expires_at=expires_at,
            ))
        mutation.claim_id = leases[0].claim_id
        mutation.claim_generation = leases[0].generation
        mutation.owner_token = token
        mutation.heartbeat_at = current_time
        mutation.lease_expires_at = expires_at
        self.db.commit()
        return WorkspaceClaimTakeover(previous, tuple(leases))

    def begin_workspace_drain(self, user_id: str) -> None:
        self._lock_user(user_id)
        workspace = (
            self.db.query(UserWorkspace)
            .filter(UserWorkspace.user_id == user_id)
            .with_for_update()
            .one_or_none()
        )
        if workspace is None:
            workspace = UserWorkspace(
                user_id=user_id,
                root_path="/home/user/workdir",
                quota_bytes=int(self.settings.workspace_quota_bytes),
                history_quota_bytes=int(self.settings.workspace_history_quota_bytes),
                status="draining",
            )
            self.db.add(workspace)
            self.db.commit()
            return
        if workspace.status != "active":
            self.db.rollback()
            raise WorkspaceDraining("工作区已经处于运行环境切换中")
        current_time = now_naive()
        self.db.query(WorkspaceClaim).filter(
            WorkspaceClaim.user_id == user_id,
            WorkspaceClaim.state == "active",
            WorkspaceClaim.mutation_id.is_(None),
            WorkspaceClaim.lease_expires_at <= current_time,
        ).update(
            {"state": "fenced", "released_at": current_time},
            synchronize_session="fetch",
        )
        claims = (
            self.db.query(WorkspaceClaim)
            .filter(
                WorkspaceClaim.user_id == user_id,
                WorkspaceClaim.state == "active",
            )
            .with_for_update()
            .all()
        )
        if claims:
            self.db.rollback()
            raise WorkspaceClaimConflict(claim.scope_key for claim in claims)
        workspace.status = "draining"
        workspace.updated_at = current_time
        self.db.commit()

    def finish_workspace_drain(self, user_id: str) -> None:
        self._lock_user(user_id)
        workspace = (
            self.db.query(UserWorkspace)
            .filter(UserWorkspace.user_id == user_id)
            .with_for_update()
            .one_or_none()
        )
        if workspace is not None and workspace.status == "draining":
            workspace.status = "active"
            workspace.updated_at = now_naive()
        self.db.commit()

    def fence_expired_claims_after_reconciliation(
        self,
        leases: Iterable[WorkspaceClaimLease],
    ) -> None:
        """Fence expired owners only after their filesystem state was reconciled."""
        current_time = now_naive()
        for lease in leases:
            updated = (
                self.db.query(WorkspaceClaim)
                .filter(
                    *self._owner_filters(lease),
                    WorkspaceClaim.state == "active",
                    WorkspaceClaim.lease_expires_at <= current_time,
                )
                .update(
                    {"state": "fenced", "released_at": current_time},
                    synchronize_session="fetch",
                )
            )
            if updated != 1:
                self.db.rollback()
                raise WorkspaceClaimLost(lease.claim_id)
        self.db.commit()

    def _lock_user(self, user_id: str) -> None:
        row = (
            self.db.query(AuthUser.user_id)
            .filter(AuthUser.user_id == user_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise ValueError(f"认证用户不存在: {user_id}")

    @staticmethod
    def _owner_filters(lease: WorkspaceClaimLease) -> tuple[Any, ...]:
        return (
            WorkspaceClaim.claim_id == lease.claim_id,
            WorkspaceClaim.user_id == lease.user_id,
            WorkspaceClaim.owner_token == lease.owner_token,
            WorkspaceClaim.generation == lease.generation,
        )

    @staticmethod
    def _normalize_specs(
        specs: Iterable[WorkspaceClaimSpec],
    ) -> list[WorkspaceClaimSpec]:
        normalized: dict[str, WorkspaceClaimSpec] = {}
        for spec in specs:
            if spec.scope_kind not in _VALID_SCOPE_KINDS:
                raise ValueError(f"未知 workspace claim scope: {spec.scope_kind}")
            scope_key = spec.scope_key.strip()
            if not scope_key or len(scope_key) > 160:
                raise ValueError("workspace claim scope_key 无效")
            normalized.setdefault(
                scope_key,
                WorkspaceClaimSpec(
                    scope_kind=spec.scope_kind,
                    scope_key=scope_key,
                    entry_id=spec.entry_id,
                    conflict_scope_keys=tuple(sorted(set(spec.conflict_scope_keys))),
                    conflict_entry_ids=tuple(sorted(set(spec.conflict_entry_ids))),
                ),
            )
        return [normalized[key] for key in sorted(normalized)]


class WorkspaceClaimHeartbeat:
    """Renew claims in independent sessions while Sandbox I/O is in progress."""

    def __init__(
        self,
        db_session_factory: Callable[[], DBSession],
        leases: Iterable[WorkspaceClaimLease],
        *,
        lease_seconds: int,
        interval_seconds: float | None = None,
    ) -> None:
        self._db_session_factory = db_session_factory
        self._leases = tuple(leases)
        self._lease_seconds = max(int(lease_seconds), 10)
        self._interval_seconds = max(
            1.0,
            float(interval_seconds or max(1, self._lease_seconds // 4)),
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._error: BaseException | None = None

    async def __aenter__(self) -> "WorkspaceClaimHeartbeat":
        if self._leases:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    def raise_if_lost(self) -> None:
        if self._error is not None:
            raise self._error

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(self._renew_once)
            except BaseException as exc:  # surfaced to the mutation before finalize
                self._error = exc
                return

    def _renew_once(self) -> None:
        db = self._db_session_factory()
        try:
            WorkspaceMutationCoordinator(db).renew_claims(
                self._leases,
                lease_seconds=self._lease_seconds,
            )
        finally:
            db.close()


@asynccontextmanager
async def keep_workspace_claims_alive(
    db_session_factory: Callable[[], DBSession],
    leases: Iterable[WorkspaceClaimLease],
    *,
    lease_seconds: int,
    interval_seconds: float | None = None,
) -> AsyncIterator[WorkspaceClaimHeartbeat]:
    heartbeat = WorkspaceClaimHeartbeat(
        db_session_factory,
        leases,
        lease_seconds=lease_seconds,
        interval_seconds=interval_seconds,
    )
    async with heartbeat:
        yield heartbeat
    heartbeat.raise_if_lost()


__all__ = [
    "WorkspaceClaimConflict",
    "WorkspaceClaimHeartbeat",
    "WorkspaceClaimLease",
    "WorkspaceClaimLost",
    "WorkspaceClaimSpec",
    "WorkspaceClaimTakeover",
    "WorkspaceDraining",
    "WorkspaceMutationCoordinator",
    "file_scope",
    "keep_workspace_claims_alive",
    "path_scope",
    "tree_scope",
    "workspace_scope",
]
