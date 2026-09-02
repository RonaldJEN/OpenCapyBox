from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.auth_user import AuthUser
from src.api.models.workspace import UserWorkspace, WorkspaceClaim
from src.api.services.workspace_mutation_coordinator import (
    WorkspaceClaimConflict,
    WorkspaceClaimLost,
    WorkspaceClaimSpec,
    WorkspaceDraining,
    WorkspaceMutationCoordinator,
    file_scope,
    path_scope,
    tree_scope,
)
from src.api.utils.timezone import now_naive


@pytest.fixture
def coordinator_db():
    engine = create_engine("sqlite://")
    AuthUser.__table__.create(engine)
    UserWorkspace.__table__.create(engine)
    WorkspaceClaim.__table__.create(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    db.add_all([
        AuthUser(user_id="user-1", username="user-1"),
        UserWorkspace(user_id="user-1", root_path="/home/user/workdir"),
    ])
    db.commit()
    try:
        yield db, Session
    finally:
        db.close()
        engine.dispose()


def test_same_file_claim_is_exclusive_and_generation_advances(coordinator_db):
    db, _Session = coordinator_db
    coordinator = WorkspaceMutationCoordinator(db)
    first = coordinator.acquire_claims(
        user_id="user-1",
        operation="write_content",
        specs=[WorkspaceClaimSpec("file", file_scope("entry-1"), "entry-1")],
    )

    with pytest.raises(WorkspaceClaimConflict):
        coordinator.acquire_claims(
            user_id="user-1",
            operation="write_content",
            specs=[WorkspaceClaimSpec("file", file_scope("entry-1"), "entry-1")],
        )

    coordinator.release_claims(first)
    second = coordinator.acquire_claims(
        user_id="user-1",
        operation="write_content",
        specs=[WorkspaceClaimSpec("file", file_scope("entry-1"), "entry-1")],
    )
    assert second[0].generation == first[0].generation + 1


def test_tree_claim_conflicts_with_existing_child_and_future_child(coordinator_db):
    db, _Session = coordinator_db
    coordinator = WorkspaceMutationCoordinator(db)
    child = coordinator.acquire_claims(
        user_id="user-1",
        operation="write_content",
        specs=[WorkspaceClaimSpec(
            "file",
            file_scope("child-1"),
            "child-1",
            conflict_scope_keys=(tree_scope("folder-1"),),
        )],
    )
    with pytest.raises(WorkspaceClaimConflict):
        coordinator.acquire_claims(
            user_id="user-1",
            operation="move_entry",
            specs=[WorkspaceClaimSpec(
                "tree",
                tree_scope("folder-1"),
                "folder-1",
                conflict_entry_ids=("folder-1", "child-1"),
            )],
        )

    coordinator.release_claims(child)
    folder = coordinator.acquire_claims(
        user_id="user-1",
        operation="move_entry",
        specs=[WorkspaceClaimSpec(
            "tree",
            tree_scope("folder-1"),
            "folder-1",
            conflict_entry_ids=("folder-1", "child-1"),
        )],
    )
    with pytest.raises(WorkspaceClaimConflict):
        coordinator.acquire_claims(
            user_id="user-1",
            operation="create_file",
            specs=[WorkspaceClaimSpec(
                "path",
                path_scope("folder-1", "new.md"),
                "folder-1",
                conflict_scope_keys=(tree_scope("folder-1"),),
            )],
        )
    coordinator.release_claims(folder)


def test_expired_claim_stays_exclusive_until_reconciler_fences_owner(coordinator_db):
    db, _Session = coordinator_db
    coordinator = WorkspaceMutationCoordinator(db)
    first = coordinator.acquire_claims(
        user_id="user-1",
        operation="write_content",
        specs=[WorkspaceClaimSpec("file", file_scope("entry-1"), "entry-1")],
    )
    row = db.get(WorkspaceClaim, first[0].claim_id)
    row.lease_expires_at = now_naive() - timedelta(seconds=1)
    db.commit()

    coordinator.renew_claims(first)
    assert db.get(WorkspaceClaim, first[0].claim_id).lease_expires_at > now_naive()
    with pytest.raises(WorkspaceClaimConflict):
        coordinator.acquire_claims(
            user_id="user-1",
            operation="write_content",
            specs=[WorkspaceClaimSpec("file", file_scope("entry-1"), "entry-1")],
        )

    row = db.get(WorkspaceClaim, first[0].claim_id)
    row.lease_expires_at = now_naive() - timedelta(seconds=1)
    db.commit()
    coordinator.fence_expired_claims_after_reconciliation(first)
    with pytest.raises(WorkspaceClaimLost):
        coordinator.renew_claims(first)
    second = coordinator.acquire_claims(
        user_id="user-1",
        operation="write_content",
        specs=[WorkspaceClaimSpec("file", file_scope("entry-1"), "entry-1")],
    )
    assert db.get(WorkspaceClaim, first[0].claim_id).state == "fenced"
    assert second[0].generation == first[0].generation + 1


def test_workspace_drain_blocks_new_claims_and_requires_no_live_owner(coordinator_db):
    db, _Session = coordinator_db
    coordinator = WorkspaceMutationCoordinator(db)
    active = coordinator.acquire_claims(
        user_id="user-1",
        operation="create_file",
        specs=[WorkspaceClaimSpec(
            "path",
            path_scope(None, "new.md"),
            None,
        )],
    )
    with pytest.raises(WorkspaceClaimConflict):
        coordinator.begin_workspace_drain("user-1")

    coordinator.release_claims(active)
    coordinator.begin_workspace_drain("user-1")
    assert db.get(UserWorkspace, "user-1").status == "draining"
    with pytest.raises(WorkspaceDraining):
        coordinator.acquire_claims(
            user_id="user-1",
            operation="create_file",
            specs=[WorkspaceClaimSpec("path", path_scope(None, "other.md"))],
        )
    coordinator.finish_workspace_drain("user-1")
    assert db.get(UserWorkspace, "user-1").status == "active"
