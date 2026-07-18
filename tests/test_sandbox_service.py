"""OpenSandbox 會話服務測試

使用 mock 替代真實的 OpenSandbox SDK，測試 SandboxSessionService 的:
- 生命週期管理（create / get_or_resume / pause / kill / renew）
- 記憶體快取
- push_skills 文件上傳
- 全局單例
"""
import asyncio
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from tests.helpers import make_mock_sandbox


# ============== Fixtures ==============

@pytest.fixture(autouse=True)
def reset_singleton():
    """每個測試前重置單例，確保隔離"""
    from src.api.services.sandbox_service import SandboxSessionService
    SandboxSessionService._instance = None
    yield
    SandboxSessionService._instance = None


@pytest.fixture
def mock_settings():
    """模擬配置"""
    with patch("src.api.services.sandbox_service.get_settings") as mock:
        s = MagicMock()
        s.sandbox_domain = "test.sandbox.io"
        s.sandbox_api_key = "test-key"
        s.sandbox_protocol = "http"
        s.sandbox_use_server_proxy = True
        s.sandbox_timeout_minutes = 10
        s.sandbox_ready_timeout_seconds = 120
        s.sandbox_image = "test-image:v1"
        s.sandbox_persistent_storage_enabled = True
        s.sandbox_host_storage_root = "/tmp/sandbox"
        s.sandbox_storage_mount_path = "/home/user"
        mock.return_value = s
        yield s


@pytest.fixture
def service(mock_settings):
    """創建 SandboxSessionService 實例"""
    from src.api.services.sandbox_service import (
        ProfileCompatibility,
        SandboxSessionService,
        _runtime_config_from_settings,
    )
    with patch.object(
        SandboxSessionService,
        "_resolve_runtime_config",
        side_effect=lambda user_id: _runtime_config_from_settings(),
    ), patch.object(
        SandboxSessionService,
        "_resolve_runtime_for_existing_sandbox",
        side_effect=lambda user_id, sandbox_id: _runtime_config_from_settings(),
    ), patch.object(
        SandboxSessionService,
        "_persisted_profile_compatibility",
        return_value=ProfileCompatibility.MATCH,
    ), patch.object(
        SandboxSessionService,
        "_query_sandbox_state",
        new_callable=AsyncMock,
        return_value="running",
    ):
        yield SandboxSessionService()


@pytest.fixture
def mock_sandbox():
    """模擬 Sandbox 實例，命令默認成功。"""
    return make_mock_sandbox(run_return=SimpleNamespace(exit_code=0))


# ============== SandboxSessionService 初始化 ==============

class TestSandboxSessionServiceInit:
    """服務初始化測試"""

    def test_singleton(self, mock_settings):
        """測試單例模式"""
        from src.api.services.sandbox_service import SandboxSessionService
        s1 = SandboxSessionService()
        s2 = SandboxSessionService()
        assert s1 is s2

    def test_initial_cache_empty(self, service):
        """測試初始快取為空"""
        assert service.cache_size == 0

    def test_get_cached_returns_none(self, service):
        """測試從空快取獲取返回 None"""
        assert service.get_cached("nonexistent") is None

    def test_get_sandbox_id_returns_none(self, service):
        """測試從空快取獲取 sandbox_id 返回 None"""
        assert service.get_sandbox_id("nonexistent") is None


# ============== create ==============

class TestCreate:
    """沙箱創建測試"""

    @pytest.mark.asyncio
    async def test_create_success(self, service, mock_sandbox):
        """測試成功創建沙箱"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            result = await service.create("session-1")

            assert result is mock_sandbox
            assert service.get_cached("session-1") is mock_sandbox
            assert service.cache_size == 1
            MockSandbox.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_failure_raises(self, service):
        """測試創建失敗拋出 RuntimeError"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(side_effect=Exception("connection refused"))

            with pytest.raises(RuntimeError, match="沙箱創建失敗"):
                await service.create("session-1")

            assert service.cache_size == 0

    @pytest.mark.asyncio
    async def test_create_stores_in_cache(self, service, mock_sandbox):
        """測試創建後存入快取"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)
            await service.create("session-1")

            assert service.get_sandbox_id("session-1") == "sbx-test-123"

    @pytest.mark.asyncio
    async def test_create_with_persistent_volume(self, service, mock_sandbox):
        """測試 create 會帶入 session 專屬持久化卷"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            await service.create("session-abc")

            kwargs = MockSandbox.create.call_args.kwargs
            assert "volumes" in kwargs
            assert kwargs["volumes"] is not None
            assert len(kwargs["volumes"]) == 1
            volume = kwargs["volumes"][0]
            assert volume.mount_path == "/home/user"
            assert volume.host is not None
            assert volume.host.path.startswith("/tmp/sandbox/")

    @pytest.mark.asyncio
    async def test_create_without_persistent_volume_when_disabled(self, service, mock_sandbox, mock_settings):
        """測試可關閉持久化卷掛載"""
        mock_settings.sandbox_persistent_storage_enabled = False
        with patch("src.api.services.sandbox_service.settings", mock_settings):
            with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
                MockSandbox.create = AsyncMock(return_value=mock_sandbox)

                await service.create("session-no-volume")

                kwargs = MockSandbox.create.call_args.kwargs
                assert kwargs.get("volumes") is None

    @pytest.mark.asyncio
    async def test_create_with_custom_mount_path(self, service, mock_sandbox, mock_settings):
        """測試自定義 mount path 會生效到 volume 配置"""
        mock_settings.sandbox_persistent_storage_enabled = True
        mock_settings.sandbox_storage_mount_path = "/workspace/session-root"
        with patch("src.api.services.sandbox_service.settings", mock_settings):
            with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
                MockSandbox.create = AsyncMock(return_value=mock_sandbox)

                await service.create("session-custom-mount")

                kwargs = MockSandbox.create.call_args.kwargs
                volume = kwargs["volumes"][0]
                assert volume.mount_path == "/workspace/session-root"


class TestPathHelpers:
    """路徑解析輔助函式測試"""

    def test_get_sandbox_mount_path_normalized(self):
        from src.api.services import sandbox_service as sandbox_module
        mock_s = MagicMock()
        mock_s.sandbox_storage_mount_path = "/workspace/app/"
        with patch.object(sandbox_module, "settings", mock_s):
            assert sandbox_module.get_sandbox_mount_path() == "/workspace/app"

    def test_resolve_sandbox_path_relative(self):
        from src.api.services.sandbox_service import resolve_sandbox_path
        result = resolve_sandbox_path("docs/readme.md", "/workspace/app")
        assert result == "/workspace/app/docs/readme.md"

    def test_resolve_sandbox_path_absolute(self):
        from src.api.services.sandbox_service import resolve_sandbox_path
        result = resolve_sandbox_path("/tmp/x.txt", "/workspace/app")
        assert result == "/tmp/x.txt"

    def test_to_sandbox_relative_path(self):
        from src.api.services.sandbox_service import to_sandbox_relative_path
        result = to_sandbox_relative_path("/workspace/app/folder/a.txt", "/workspace/app")
        assert result == "folder/a.txt"

    def test_is_within_sandbox_root(self):
        from src.api.services.sandbox_service import is_within_sandbox_root
        assert is_within_sandbox_root("/workspace/app/a.txt", "/workspace/app") is True
        assert is_within_sandbox_root("/tmp/a.txt", "/workspace/app") is False


# ============== get_or_resume ==============

class TestGetOrResume:
    """獲取/恢復沙箱測試"""

    @pytest.mark.asyncio
    async def test_cache_hit_healthy(self, service, mock_sandbox):
        """測試快取命中且健康"""
        service._cache["session-1"] = mock_sandbox

        result = await service.get_or_resume("session-1")

        assert result is mock_sandbox
        mock_sandbox.is_healthy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_hit_with_different_persisted_id_invalidates_and_connects(
        self, service, mock_sandbox
    ):
        """持久化 sandbox_id 與快取不一致時，不得返回舊快取。"""
        stale_sandbox = make_mock_sandbox(sandbox_id="sbx-old")
        service._cache["session-1"] = stale_sandbox
        service._pushed_skills["session-1"] = {"example-skill"}

        with (
            patch.object(
                service,
                "_query_sandbox_state",
                new=AsyncMock(return_value="running"),
            ) as query_state,
            patch("src.api.services.sandbox_service.Sandbox") as MockSandbox,
        ):
            MockSandbox.connect = AsyncMock(return_value=mock_sandbox)

            result = await service.get_or_resume("session-1", "sbx-test-123")

        assert result is mock_sandbox
        stale_sandbox.is_healthy.assert_not_awaited()
        query_state.assert_awaited_once_with("sbx-test-123", ANY)
        MockSandbox.connect.assert_awaited_once()
        assert service.get_cached("session-1") is mock_sandbox
        assert service._pushed_skills["session-1"] == set()

    @pytest.mark.asyncio
    async def test_cache_hit_unhealthy_falls_to_connect(self, service, mock_sandbox):
        """測試快取命中但不健康 -> 優先嘗試 connect"""
        unhealthy = AsyncMock()
        unhealthy.is_healthy = AsyncMock(return_value=False)
        service._cache["session-1"] = unhealthy

        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(return_value=mock_sandbox)
            result = await service.get_or_resume("session-1", "sbx-old-id")

        assert result is mock_sandbox
        assert service.get_cached("session-1") is mock_sandbox

    @pytest.mark.asyncio
    async def test_connect_from_sandbox_id(self, service, mock_sandbox):
        """測試通過 sandbox_id connect"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(return_value=mock_sandbox)

            result = await service.get_or_resume("session-1", "sbx-old-id")

            assert result is mock_sandbox
            MockSandbox.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_profile_fingerprint_skips_connect_and_creates_new(self, service, mock_sandbox):
        """Profile 指紋過期時不得復用舊 sandbox_id。"""
        from src.api.services.sandbox_service import ProfileCompatibility, SandboxSessionService

        with patch.object(
            SandboxSessionService,
            "_persisted_profile_compatibility",
            return_value=ProfileCompatibility.MISMATCH,
        ), patch.object(
            service,
            "_compare_and_swap_sandbox_binding",
            return_value=(True, mock_sandbox.id),
        ):
            with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
                MockSandbox.connect = AsyncMock()
                MockSandbox.resume = AsyncMock()
                MockSandbox.create = AsyncMock(return_value=mock_sandbox)

                result = await service.get_or_resume("session-1", "sbx-old-id")

                assert result is mock_sandbox
                MockSandbox.connect.assert_not_awaited()
                MockSandbox.resume.assert_not_awaited()
                MockSandbox.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_failure_does_not_resume_or_create(self, service):
        """Running 状态连接失败只能暂时不可用，不能继续 resume/create。"""
        from src.api.services.sandbox_service import SandboxTemporarilyUnavailable

        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(side_effect=Exception("connect failed"))
            MockSandbox.resume = AsyncMock()
            MockSandbox.create = AsyncMock()

            with pytest.raises(SandboxTemporarilyUnavailable):
                await service.get_or_resume("session-1", "sbx-old-id")

            MockSandbox.resume.assert_not_awaited()
            MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_failure_does_not_create_new(self, service):
        """Paused 状态恢复失败只能暂时不可用，不能创建替代沙箱。"""
        from src.api.services.sandbox_service import SandboxTemporarilyUnavailable

        with patch.object(
            service,
            "_query_sandbox_state",
            new=AsyncMock(return_value="paused"),
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock()
            MockSandbox.resume = AsyncMock(side_effect=Exception("resume timeout"))
            MockSandbox.create = AsyncMock()

            with pytest.raises(SandboxTemporarilyUnavailable):
                await service.get_or_resume("session-1", "sbx-old-id")

            MockSandbox.connect.assert_not_awaited()
            MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["terminated", "failed", "not_found"])
    async def test_terminal_state_rebuilds_with_cas(self, service, mock_sandbox, state):
        """只有明确终态才创建候选沙箱并条件更新绑定。"""
        with patch.object(
            service,
            "_query_sandbox_state",
            new=AsyncMock(return_value=state),
        ), patch.object(
            service,
            "_compare_and_swap_sandbox_binding",
            return_value=(True, mock_sandbox.id),
        ) as cas, patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock()
            MockSandbox.resume = AsyncMock()
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            result = await service.recover_persisted_sandbox("session-1", "sbx-old-id")

        assert result is mock_sandbox
        MockSandbox.connect.assert_not_awaited()
        MockSandbox.resume.assert_not_awaited()
        MockSandbox.create.assert_awaited_once()
        cas.assert_called_once_with(
            "session-1",
            "sbx-old-id",
            mock_sandbox.id,
            runtime_config=ANY,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["pausing", "stopping", "unknown", "mystery"])
    async def test_transitional_or_unknown_state_never_creates(self, service, state):
        from src.api.services.sandbox_service import SandboxTemporarilyUnavailable

        with patch.object(
            service,
            "_query_sandbox_state",
            new=AsyncMock(return_value=state),
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock()
            MockSandbox.resume = AsyncMock()
            MockSandbox.create = AsyncMock()

            with pytest.raises(SandboxTemporarilyUnavailable):
                await service.recover_persisted_sandbox("session-1", "sbx-old-id")

        MockSandbox.connect.assert_not_awaited()
        MockSandbox.resume.assert_not_awaited()
        MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_state_query_failure_never_creates(self, service):
        from src.api.services.sandbox_service import SandboxTemporarilyUnavailable

        with patch.object(
            service,
            "_query_sandbox_state",
            new=AsyncMock(side_effect=SandboxTemporarilyUnavailable("network")),
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock()

            with pytest.raises(SandboxTemporarilyUnavailable):
                await service.recover_persisted_sandbox("session-1", "sbx-old-id")

        MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_paused_resume_keeps_same_id(self, service):
        resumed = make_mock_sandbox(sandbox_id="sbx-old-id")
        with patch.object(
            service,
            "_query_sandbox_state",
            new=AsyncMock(return_value="paused"),
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock()
            MockSandbox.resume = AsyncMock(return_value=resumed)
            MockSandbox.create = AsyncMock()

            result = await service.recover_persisted_sandbox("session-1", "sbx-old-id")

        assert result.id == "sbx-old-id"
        MockSandbox.resume.assert_awaited_once()
        MockSandbox.connect.assert_not_awaited()
        MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_profile_unknown_never_rebuilds(self, service):
        from src.api.services.sandbox_service import (
            ProfileCompatibility,
            SandboxTemporarilyUnavailable,
        )

        with patch.object(
            service,
            "_persisted_profile_compatibility",
            return_value=ProfileCompatibility.UNKNOWN,
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock()

            with pytest.raises(SandboxTemporarilyUnavailable):
                await service.recover_persisted_sandbox("session-1", "sbx-old-id")

        MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_requested_id_reuses_current_binding(self, service):
        from src.api.services.sandbox_service import ProfileCompatibility

        winner = make_mock_sandbox(sandbox_id="sbx-winner")
        with patch.object(
            service,
            "_persisted_profile_compatibility",
            side_effect=[ProfileCompatibility.STALE_BINDING, ProfileCompatibility.MATCH],
        ), patch.object(
            service,
            "_read_persisted_sandbox_id",
            return_value="sbx-winner",
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(return_value=winner)
            MockSandbox.resume = AsyncMock()
            MockSandbox.create = AsyncMock()

            result = await service.recover_persisted_sandbox("session-1", "sbx-old-id")

        assert result is winner
        MockSandbox.connect.assert_awaited_once()
        MockSandbox.resume.assert_not_awaited()
        MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rebuild_cas_loser_destroys_only_container_and_uses_winner(
        self, service
    ):
        candidate = make_mock_sandbox(sandbox_id="sbx-candidate")
        winner = make_mock_sandbox(sandbox_id="sbx-winner")
        with patch.object(
            service,
            "_query_sandbox_state",
            new=AsyncMock(side_effect=["terminated", "running"]),
        ), patch.object(
            service,
            "_compare_and_swap_sandbox_binding",
            return_value=(False, "sbx-winner"),
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=candidate)
            MockSandbox.connect = AsyncMock(return_value=winner)
            MockSandbox.resume = AsyncMock()

            result = await service.recover_persisted_sandbox("session-1", "sbx-old-id")

        assert result is winner
        candidate.kill.assert_awaited_once()
        candidate.close.assert_awaited_once()
        candidate.commands.run.assert_not_awaited()
        MockSandbox.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_existing_never_creates_replacement(self, service):
        with patch.object(
            service,
            "_query_sandbox_state",
            new=AsyncMock(return_value="terminated"),
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock()

            with pytest.raises(RuntimeError, match="既有沙箱不可用"):
                await service.get_existing("session-1", "sbx-old-id")

            MockSandbox.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_existing_rejects_different_cached_generation(
        self, service, mock_sandbox
    ):
        mock_sandbox.id = "sbx-current"
        mock_sandbox.is_healthy = AsyncMock(return_value=True)
        service._cache["session-1"] = mock_sandbox
        service._cache_profile_ids["session-1"] = "env-default"
        service._cache_profile_versions["session-1"] = 1

        from src.api.services.sandbox_service import ProfileCompatibility

        with patch.object(
            service,
            "_persisted_profile_compatibility",
            return_value=ProfileCompatibility.MISMATCH,
        ):
            with pytest.raises(RuntimeError, match="profile 指纹不匹配"):
                await service.get_existing("session-1", "sbx-old")

        assert service.get_cached("session-1") is mock_sandbox

    @pytest.mark.asyncio
    async def test_get_existing_accepts_matching_live_cache_before_persistence(
        self, service, mock_sandbox
    ):
        mock_sandbox.id = "sbx-current"
        mock_sandbox.is_healthy = AsyncMock(return_value=True)
        service._cache["session-1"] = mock_sandbox
        service._cache_profile_ids["session-1"] = "env-default"
        service._cache_profile_versions["session-1"] = 1

        from src.api.services.sandbox_service import ProfileCompatibility

        with patch.object(
            service,
            "_persisted_profile_compatibility",
            return_value=ProfileCompatibility.UNKNOWN,
        ):
            result = await service.get_existing("session-1", "sbx-current")

        assert result is mock_sandbox

    @pytest.mark.asyncio
    async def test_no_sandbox_id_creates_new(self, service, mock_sandbox):
        """測試沒有 sandbox_id -> 直接創建"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            result = await service.get_or_resume("session-1", None)

            assert result is mock_sandbox
            MockSandbox.create.assert_awaited_once()


class TestSandboxBindingCas:
    @staticmethod
    def _session_factory():
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from src.api.models.database import Base
        from src.api.models.user_sandbox import UserSandbox

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine, tables=[UserSandbox.__table__])
        return sessionmaker(bind=engine)

    def test_only_first_rebuild_candidate_can_replace_old_binding(self, mock_settings):
        from src.api.models.user_sandbox import UserSandbox
        from src.api.services.sandbox_service import (
            SandboxSessionService,
            _runtime_config_from_settings,
        )

        Session = self._session_factory()
        with Session() as db:
            db.add(UserSandbox(
                id="binding-1",
                user_id="user-1",
                sandbox_id="sbx-old",
                active_profile_id="env-default",
                active_profile_version=1,
                status="active",
            ))
            db.commit()

        runtime_config = _runtime_config_from_settings()
        with patch("src.api.models.database.SessionLocal", Session):
            first = SandboxSessionService._compare_and_swap_sandbox_binding(
                "user-1",
                "sbx-old",
                "sbx-new-a",
                runtime_config=runtime_config,
            )
            second = SandboxSessionService._compare_and_swap_sandbox_binding(
                "user-1",
                "sbx-old",
                "sbx-new-b",
                runtime_config=runtime_config,
            )

        assert first == (True, "sbx-new-a")
        assert second == (False, "sbx-new-a")
        with Session() as db:
            assert db.query(UserSandbox).filter(UserSandbox.user_id == "user-1").one().sandbox_id == "sbx-new-a"

    def test_profile_compatibility_distinguishes_mismatch_from_query_failure(
        self, mock_settings
    ):
        from src.api.models.user_sandbox import UserSandbox
        from src.api.services.sandbox_service import (
            ProfileCompatibility,
            SandboxSessionService,
            _runtime_config_from_settings,
        )

        Session = self._session_factory()
        with Session() as db:
            db.add(UserSandbox(
                id="binding-1",
                user_id="user-1",
                sandbox_id="sbx-old",
                active_profile_id="env-default",
                active_profile_version=1,
                status="active",
            ))
            db.commit()

        runtime_config = _runtime_config_from_settings()
        with patch("src.api.models.database.SessionLocal", Session):
            assert SandboxSessionService._persisted_profile_compatibility(
                "user-1", "sbx-old", runtime_config
            ) == ProfileCompatibility.MATCH

            assert SandboxSessionService._persisted_profile_compatibility(
                "user-1", "sbx-stale", runtime_config
            ) == ProfileCompatibility.STALE_BINDING

            with Session() as db:
                row = db.query(UserSandbox).filter(UserSandbox.user_id == "user-1").one()
                row.active_profile_version = 2
                db.commit()

            assert SandboxSessionService._persisted_profile_compatibility(
                "user-1", "sbx-old", runtime_config
            ) == ProfileCompatibility.MISMATCH

        with patch("src.api.models.database.SessionLocal", side_effect=RuntimeError("db down")):
            assert SandboxSessionService._persisted_profile_compatibility(
                "user-1", "sbx-old", runtime_config
            ) == ProfileCompatibility.UNKNOWN


# ============== pause ==============

class TestPause:
    """暫停沙箱測試"""

    @pytest.mark.asyncio
    async def test_pause_success(self, service, mock_sandbox):
        """測試成功暫停"""
        service._cache["session-1"] = mock_sandbox

        result = await service.pause("session-1")

        assert result is True
        mock_sandbox.pause.assert_awaited_once()
        assert service.get_cached("session-1") is None

    @pytest.mark.asyncio
    async def test_pause_not_in_cache(self, service):
        """測試暫停不在快取中的沙箱"""
        result = await service.pause("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_pause_failure(self, service, mock_sandbox):
        """測試暫停失敗"""
        mock_sandbox.pause = AsyncMock(side_effect=Exception("network error"))
        service._cache["session-1"] = mock_sandbox

        result = await service.pause("session-1")

        assert result is False
        # 即使暫停失敗，也應該從快取中移除
        assert service.get_cached("session-1") is None


# ============== kill ==============

class TestKill:
    """銷毀沙箱測試"""

    @pytest.mark.asyncio
    async def test_kill_from_cache(self, service, mock_sandbox):
        """測試從快取中銷毀（含文件清理）"""
        service._cache["session-1"] = mock_sandbox

        result = await service.kill("session-1")

        assert result is True
        # 應先執行 rm -rf 清理命令
        mock_sandbox.commands.run.assert_awaited_once()
        cmd = mock_sandbox.commands.run.call_args[0][0]
        assert "rm -rf" in cmd
        assert "|| true" not in cmd
        # 再執行 kill
        mock_sandbox.kill.assert_awaited_once()
        assert service.get_cached("session-1") is None

    @pytest.mark.asyncio
    async def test_kill_by_sandbox_id(self, service, mock_sandbox):
        """測試通過 sandbox_id 銷毀（不在快取中）"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(return_value=mock_sandbox)

            result = await service.kill("session-1", "sbx-old-id")

            assert result is True
            # 應先清理文件再 kill
            mock_sandbox.commands.run.assert_awaited_once()
            mock_sandbox.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_no_sandbox(self, service):
        """測試銷毀不存在的沙箱"""
        result = await service.kill("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_kill_connect_failure_then_resume_failure(self, service):
        """測試銷毀時 connect 和 resume 都失敗"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(side_effect=Exception("not found"))
            MockSandbox.resume = AsyncMock(side_effect=Exception("not found"))

            result = await service.kill("session-1", "sbx-dead-id")
            assert result is False

    @pytest.mark.asyncio
    async def test_kill_by_sandbox_id_returns_false_when_profile_unresolvable(self, service):
        """既有 sandbox 的 active profile 无法解析时不得回退到当前配置。"""
        from src.api.services.sandbox_service import SandboxSessionService

        with patch.object(
            SandboxSessionService,
            "_resolve_runtime_for_existing_sandbox",
            side_effect=RuntimeError("profile missing"),
        ), patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock()
            MockSandbox.resume = AsyncMock()

            result = await service.kill("session-1", "sbx-profile-missing")

        assert result is False
        MockSandbox.connect.assert_not_called()
        MockSandbox.resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_kill_connect_fails_resume_succeeds(self, service, mock_sandbox):
        """測試 connect 失敗後 resume 成功，仍能清理文件並銷毀"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(side_effect=Exception("expired"))
            MockSandbox.resume = AsyncMock(return_value=mock_sandbox)

            result = await service.kill("session-1", "sbx-expired-id")

            assert result is True
            # 應先清理文件
            mock_sandbox.commands.run.assert_awaited_once()
            cmd = mock_sandbox.commands.run.call_args[0][0]
            assert "rm -rf" in cmd
            assert "|| true" not in cmd
            # 再 kill
            mock_sandbox.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_cleanup_nonzero_exit_returns_false(self, service, mock_sandbox):
        """清理命令返回非零退出碼時不得銷毀容器。"""
        mock_sandbox.commands.run = AsyncMock(return_value=SimpleNamespace(exit_code=23))
        service._cache["session-1"] = mock_sandbox

        result = await service.kill("session-1")

        assert result is False
        mock_sandbox.kill.assert_not_awaited()
        mock_sandbox.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_cleanup_failure_returns_false(self, service, mock_sandbox):
        """清理命令失敗時不得報告銷毀成功。"""
        mock_sandbox.commands.run = AsyncMock(side_effect=RuntimeError("command failed"))
        service._cache["session-1"] = mock_sandbox

        result = await service.kill("session-1")

        assert result is False
        mock_sandbox.kill.assert_not_awaited()
        mock_sandbox.close.assert_awaited_once()


class TestResolveRuntimeForExistingSandbox:
    class _FakeQuery:
        def __init__(self, result):
            self.result = result

        def filter(self, *args):
            return self

        def first(self):
            return self.result

    class _FakeDB:
        def __init__(self, user_sandbox=None, profile=None):
            self.user_sandbox = user_sandbox
            self.profile = profile

        def query(self, model):
            if model.__name__ == "UserSandbox":
                return TestResolveRuntimeForExistingSandbox._FakeQuery(self.user_sandbox)
            if model.__name__ == "SandboxProfile":
                return TestResolveRuntimeForExistingSandbox._FakeQuery(self.profile)
            raise AssertionError(f"unexpected model: {model}")

    class _FakeSessionLocal:
        def __init__(self, db):
            self.db = db

        def __call__(self):
            return self

        def __enter__(self):
            return self.db

        def __exit__(self, exc_type, exc, tb):
            return False

    def test_missing_active_profile_does_not_fallback_to_current_profile(self, mock_settings):
        from src.api.services.sandbox_service import SandboxSessionService

        fake_db = self._FakeDB(
            user_sandbox=SimpleNamespace(
                sandbox_id="sbx-1",
                active_profile_id="missing-profile",
                active_profile_version=1,
            ),
            profile=None,
        )
        with patch("src.api.models.database.SessionLocal", self._FakeSessionLocal(fake_db)), patch.object(
            SandboxSessionService,
            "_resolve_runtime_config",
        ) as fallback:
            with pytest.raises(RuntimeError):
                SandboxSessionService._resolve_runtime_for_existing_sandbox("user-1", "sbx-1")

        fallback.assert_not_called()

    def test_active_profile_version_mismatch_is_not_cleanable(self, mock_settings):
        from src.api.services.sandbox_service import SandboxSessionService

        fake_db = self._FakeDB(
            user_sandbox=SimpleNamespace(
                sandbox_id="sbx-1",
                active_profile_id="profile-a",
                active_profile_version=1,
            ),
            profile=SimpleNamespace(id="profile-a", version=2),
        )
        with patch("src.api.models.database.SessionLocal", self._FakeSessionLocal(fake_db)), patch.object(
            SandboxSessionService,
            "_resolve_runtime_config",
        ) as fallback:
            with pytest.raises(RuntimeError):
                SandboxSessionService._resolve_runtime_for_existing_sandbox("user-1", "sbx-1")

        fallback.assert_not_called()


# ============== renew ==============

class TestRenew:
    """續租沙箱測試"""

    @pytest.mark.asyncio
    async def test_renew_success(self, service, mock_sandbox):
        """測試成功續租"""
        service._cache["session-1"] = mock_sandbox

        result = await service.renew("session-1")

        assert result is True
        mock_sandbox.renew.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_renew_not_in_cache(self, service):
        """測試續租不在快取中的沙箱"""
        result = await service.renew("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_renew_failure(self, service, mock_sandbox):
        """測試續租失敗"""
        mock_sandbox.renew = AsyncMock(side_effect=Exception("timeout"))
        service._cache["session-1"] = mock_sandbox

        result = await service.renew("session-1")
        assert result is False


# ============== push_skills ==============

class TestPushSkills:
    """Skills 推送測試"""

    @pytest.mark.asyncio
    async def test_push_skills_success(self, service, mock_sandbox, tmp_path):
        """測試成功推送 skills"""
        service._cache["session-1"] = mock_sandbox

        # 創建測試 skill 目錄
        skill_dir = tmp_path / "skills" / "docx"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Docx Skill")

        result = await service.push_skills("session-1", str(tmp_path / "skills"))

        assert result is True
        mock_sandbox.files.write_files.assert_awaited_once()
        call_args = mock_sandbox.files.write_files.call_args[0][0]
        assert len(call_args) == 1
        from src.api.services.sandbox_service import get_sandbox_mount_path
        assert call_args[0].path == f"{get_sandbox_mount_path()}/skills/docx/SKILL.md"

    @pytest.mark.asyncio
    async def test_push_skills_not_in_cache(self, service, tmp_path):
        """測試沙箱不在快取中"""
        result = await service.push_skills("session-1", str(tmp_path))
        assert result is False

    @pytest.mark.asyncio
    async def test_push_skills_dir_not_exists(self, service, mock_sandbox):
        """測試 skills 目錄不存在"""
        service._cache["session-1"] = mock_sandbox

        result = await service.push_skills("session-1", "/nonexistent/path")
        assert result is False

    @pytest.mark.asyncio
    async def test_push_skills_skips_node_modules(self, service, mock_sandbox, tmp_path):
        """測試跳過 node_modules 目錄"""
        service._cache["session-1"] = mock_sandbox

        skill_dir = tmp_path / "skills" / "docx"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill")
        nm_dir = skill_dir / "node_modules" / "pkg"
        nm_dir.mkdir(parents=True)
        (nm_dir / "index.js").write_text("module.exports = {}")

        result = await service.push_skills("session-1", str(tmp_path / "skills"))

        assert result is True
        # 只上傳 SKILL.md，不上傳 node_modules
        call_args = mock_sandbox.files.write_files.call_args[0][0]
        assert len(call_args) == 1
        assert "node_modules" not in call_args[0].path

    @pytest.mark.asyncio
    async def test_push_skills_empty_dir(self, service, mock_sandbox, tmp_path):
        """測試空 skills 目錄"""
        service._cache["session-1"] = mock_sandbox
        skills_dir = tmp_path / "empty_skills"
        skills_dir.mkdir()

        result = await service.push_skills("session-1", str(skills_dir))
        assert result is True


class TestPushSkillLazy:
    """按需推送單一 skill 測試"""

    @pytest.mark.asyncio
    async def test_push_skill_success(self, service, mock_sandbox, tmp_path):
        service._cache["session-1"] = mock_sandbox

        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "document-skills" / "pdf"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: pdf\ndescription: pdf skill\n---\n")
        (scripts_dir / "read_pdf.py").write_text("print('ok')")

        result = await service.push_skill("session-1", str(skills_root), "pdf")

        assert result is True
        mock_sandbox.files.write_files.assert_awaited_once()
        entries = mock_sandbox.files.write_files.call_args[0][0]
        paths = [entry.path for entry in entries]
        from src.api.services.sandbox_service import get_sandbox_mount_path
        skills_root = f"{get_sandbox_mount_path()}/skills/document-skills/pdf"
        assert f"{skills_root}/SKILL.md" in paths
        assert f"{skills_root}/scripts/read_pdf.py" in paths

    @pytest.mark.asyncio
    async def test_push_skill_skip_when_already_pushed(self, service, mock_sandbox, tmp_path):
        service._cache["session-1"] = mock_sandbox
        service._pushed_skills["session-1"] = {"pdf"}

        result = await service.push_skill("session-1", str(tmp_path), "pdf")

        assert result is True
        mock_sandbox.files.write_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_push_skill_not_found(self, service, mock_sandbox, tmp_path):
        service._cache["session-1"] = mock_sandbox
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        result = await service.push_skill("session-1", str(skills_root), "unknown")

        assert result is False

    @pytest.mark.asyncio
    async def test_push_skill_rechecks_enabled_before_cached_shortcut(
        self, service, mock_sandbox, tmp_path
    ):
        service._cache["session-1"] = mock_sandbox
        service._pushed_skills["session-1"] = {"pdf"}

        result = await service.push_skill(
            "session-1",
            str(tmp_path),
            "pdf",
            enabled_check=lambda: False,
        )

        assert result is False
        mock_sandbox.files.write_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_push_skill_keeps_files_when_disabled_during_upload(
        self, service, mock_sandbox, tmp_path
    ):
        service._cache["session-1"] = mock_sandbox
        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "document-skills" / "pdf"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: pdf skill\n---\n"
        )
        enabled_states = iter([True, False])

        result = await service.push_skill(
            "session-1",
            str(skills_root),
            "pdf",
            enabled_check=lambda: next(enabled_states),
        )

        assert result is False
        mock_sandbox.files.write_files.assert_awaited_once()
        mock_sandbox.commands.run.assert_not_awaited()
        assert "pdf" in service._pushed_skills["session-1"]


# ============== get_sandbox_service ==============

class TestGetSandboxService:
    """全局服務存取測試"""

    def test_get_sandbox_service_returns_instance(self, mock_settings):
        """測試獲取全局服務實例"""
        from src.api.services import sandbox_service as mod
        mod._sandbox_service = None  # 重置

        svc = mod.get_sandbox_service()
        assert isinstance(svc, mod.SandboxSessionService)

    def test_get_sandbox_service_is_stable(self, mock_settings):
        """測試多次調用返回同一實例"""
        from src.api.services import sandbox_service as mod
        mod._sandbox_service = None

        svc1 = mod.get_sandbox_service()
        svc2 = mod.get_sandbox_service()
        assert svc1 is svc2


# ============== discover_sandbox_skills ==============

class TestDiscoverSandboxSkills:
    """沙箱端用戶 Skill 發現測試"""

    @pytest.fixture
    def sandbox_with_skills(self, mock_sandbox):
        """模擬沙箱中含有用戶 Skill 的場景"""
        # find 命令返回 SKILL.md 路徑列表
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.error = None
        exec_result.logs = MagicMock()
        exec_result.logs.stdout = (
            "/home/user/skills/industry-report/SKILL.md\n"
            "/home/user/skills/custom-tool/SKILL.md\n"
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        # read_file 依次返回兩個 SKILL.md 的內容
        skill_contents = {
            "/home/user/skills/industry-report/SKILL.md": (
                "---\nname: industry-report\ndisplay_name: 行业报告"
                "\ndescription: 行业研究报告\n---\n## Usage\n"
            ),
            "/home/user/skills/custom-tool/SKILL.md": (
                "---\nname: custom-tool\nmetadata:\n  display_name: Custom Tool UI"
                "\ndescription: Custom tool\n---\n## Custom\n"
            ),
        }

        async def _read_file(path):
            return skill_contents.get(path, "")

        mock_sandbox.files.read_file = AsyncMock(side_effect=_read_file)
        return mock_sandbox

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_basic(self, service, sandbox_with_skills):
        """測試基本發現功能"""
        service._cache["user-1"] = sandbox_with_skills

        results = await service.discover_sandbox_skills("user-1")

        assert len(results) == 2
        names = {r["name"] for r in results}
        assert "industry-report" in names
        assert "custom-tool" in names
        assert results[0]["sandbox_skill_dir"] == "/home/user/skills/industry-report"
        by_name = {item["name"]: item for item in results}
        assert by_name["industry-report"]["display_name"] == "行业报告"
        assert by_name["custom-tool"]["display_name"] == "Custom Tool UI"

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_dedup_official(self, service, sandbox_with_skills):
        """測試去除與官方同名的 Skill"""
        service._cache["user-1"] = sandbox_with_skills

        results = await service.discover_sandbox_skills(
            "user-1",
            official_skill_names={"custom-tool"},
        )

        assert len(results) == 1
        assert results[0]["name"] == "industry-report"

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_sandbox_not_cached(self, service):
        """測試沙箱不在快取中返回空列表"""
        results = await service.discover_sandbox_skills("user-not-exist")
        assert results == []

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_find_fails(self, service, mock_sandbox):
        """測試 find 命令失敗時返回空列表"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.commands.run = AsyncMock(side_effect=Exception("timeout"))

        results = await service.discover_sandbox_skills("user-1")
        assert results == []

        with pytest.raises(RuntimeError, match="用户 Skill 发现失败"):
            await service.discover_sandbox_skills("user-1", strict=True)

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_no_skills(self, service, mock_sandbox):
        """測試沙箱中無 Skill 時返回空列表"""
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.error = None
        exec_result.logs = MagicMock()
        exec_result.logs.stdout = ""
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        results = await service.discover_sandbox_skills("user-1")
        assert results == []

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_logs_none(self, service, mock_sandbox):
        """測試 find 命令返回 logs=None（靜默失敗）"""
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.error = None
        exec_result.logs = None
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        results = await service.discover_sandbox_skills("user-1")
        assert results == []

        with pytest.raises(RuntimeError, match="用户 Skill 发现失败"):
            await service.discover_sandbox_skills("user-1", strict=True)

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_strict_rejects_execution_error(
        self, service, mock_sandbox
    ):
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.commands.run = AsyncMock(return_value=SimpleNamespace(
            exit_code=None,
            error="remote execution failed",
            logs=None,
        ))

        with pytest.raises(RuntimeError, match="用户 Skill 发现失败"):
            await service.discover_sandbox_skills("user-1", strict=True)

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_read_file_fails(self, service, mock_sandbox):
        """測試單個 SKILL.md 讀取失敗時跳過，不影響其他"""
        service._cache["user-1"] = mock_sandbox

        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.error = None
        exec_result.logs = MagicMock()
        exec_result.logs.stdout = (
            "/home/user/skills/good/SKILL.md\n"
            "/home/user/skills/bad/SKILL.md\n"
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        async def _read_file(path):
            if "bad" in path:
                raise IOError("read failed")
            return "---\nname: good-skill\ndescription: Good\n---\ncontent"

        mock_sandbox.files.read_file = AsyncMock(side_effect=_read_file)

        results = await service.discover_sandbox_skills("user-1")
        assert len(results) == 1
        assert results[0]["name"] == "good-skill"

        with pytest.raises(RuntimeError, match="读取用户 Skill 失败"):
            await service.discover_sandbox_skills("user-1", strict=True)

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_reads_skill_files_concurrently(self, service, mock_sandbox):
        """讀取多個 SKILL.md 時應並發發起沙箱文件請求"""
        service._cache["user-1"] = mock_sandbox

        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.error = None
        exec_result.logs = MagicMock()
        exec_result.logs.stdout = (
            "/home/user/skills/first/SKILL.md\n"
            "/home/user/skills/second/SKILL.md\n"
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        all_reads_started = asyncio.Event()
        read_paths: list[str] = []

        async def _read_file(path):
            read_paths.append(path)
            if len(read_paths) == 2:
                all_reads_started.set()
            await all_reads_started.wait()
            if "first" in path:
                return "---\nname: first-skill\ndescription: First\n---\ncontent"
            return b"---\nname: second-skill\ndescription: Second\n---\ncontent"

        mock_sandbox.files.read_file = AsyncMock(side_effect=_read_file)

        results = await asyncio.wait_for(service.discover_sandbox_skills("user-1"), timeout=2)

        assert read_paths == [
            "/home/user/skills/first/SKILL.md",
            "/home/user/skills/second/SKILL.md",
        ]
        assert [item["name"] for item in results] == ["first-skill", "second-skill"]

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_rejects_duplicate_user_keys_as_batch(
        self, service, mock_sandbox
    ):
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock(exit_code=0, error=None)
        exec_result.logs = MagicMock(
            stdout=(
                "/home/user/skills/first/SKILL.md\n"
                "/home/user/skills/second/SKILL.md\n"
            )
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)
        mock_sandbox.files.read_file = AsyncMock(
            return_value="---\nname: duplicate\ndescription: Same key\n---\n"
        )

        assert await service.discover_sandbox_skills("user-1") == []
        with pytest.raises(RuntimeError, match="用户 Skill 清单无效"):
            await service.discover_sandbox_skills("user-1", strict=True)

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_rejects_unsafe_key_as_batch(
        self, service, mock_sandbox
    ):
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock(exit_code=0, error=None)
        exec_result.logs = MagicMock(
            stdout="/home/user/skills/unsafe/SKILL.md\n"
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)
        mock_sandbox.files.read_file = AsyncMock(
            return_value="---\nname: unsafe#key\ndescription: Unsafe\n---\n"
        )

        with pytest.raises(RuntimeError, match="用户 Skill 元数据无效"):
            await service.discover_sandbox_skills("user-1", strict=True)

    @pytest.mark.asyncio
    async def test_strict_discovery_rejects_invalid_frontmatter_without_partial_publish(
        self, service, mock_sandbox
    ):
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock(exit_code=0, error=None)
        exec_result.logs = MagicMock(
            stdout=(
                "/home/user/skills/good/SKILL.md\n"
                "/home/user/skills/missing-name/SKILL.md\n"
            )
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        async def _read_file(path):
            if "/good/" in path:
                return "---\nname: good\ndescription: Good\n---\n"
            return "---\ndescription: Missing name\n---\n"

        mock_sandbox.files.read_file = AsyncMock(side_effect=_read_file)

        with pytest.raises(RuntimeError, match="用户 Skill 元数据无效"):
            await service.discover_sandbox_skills("user-1", strict=True)
        assert await service.discover_sandbox_skills("user-1") == [{
            "name": "good",
            "display_name": "good",
            "description": "Good",
            "sandbox_skill_dir": "/home/user/skills/good",
        }]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "frontmatter",
        [
            "---\nname: [broken\n---\n",
            "---\nname: [not, a, string]\ndescription: Bad\n---\n",
            "---\nname: bad-description\ndescription: [not, text]\n---\n",
            "---\nname: bad-metadata\ndescription: Bad\nmetadata: scalar\n---\n",
            "---\nname: bad-display\ndescription: Bad\ndisplay_name: 42\n---\n",
        ],
    )
    async def test_strict_discovery_rejects_invalid_yaml_metadata_types(
        self, service, mock_sandbox, frontmatter
    ):
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock(exit_code=0, error=None)
        exec_result.logs = MagicMock(
            stdout="/home/user/skills/invalid/SKILL.md\n"
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)
        mock_sandbox.files.read_file = AsyncMock(return_value=frontmatter)

        with pytest.raises(RuntimeError, match="用户 Skill 元数据无效"):
            await service.discover_sandbox_skills("user-1", strict=True)

    @pytest.mark.asyncio
    async def test_official_skills_do_not_consume_user_inventory_capacity(
        self, service, mock_sandbox
    ):
        from src.api.services.skill_inventory_service import (
            MAX_USER_SKILL_INVENTORY_ITEMS,
        )

        service._cache["user-1"] = mock_sandbox
        paths = ["/home/user/skills/official/SKILL.md"] + [
            f"/home/user/skills/user-{index}/SKILL.md"
            for index in range(MAX_USER_SKILL_INVENTORY_ITEMS)
        ]
        exec_result = MagicMock(exit_code=0, error=None)
        exec_result.logs = MagicMock(stdout="\n".join(paths))
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        async def _read_file(path):
            if "/official/" in path:
                name = "official"
            else:
                name = path.split("/")[-2]
            return f"---\nname: {name}\ndescription: Test\n---\n"

        mock_sandbox.files.read_file = AsyncMock(side_effect=_read_file)
        results = await service.discover_sandbox_skills(
            "user-1",
            official_skill_names={"official"},
            strict=True,
        )

        assert len(results) == MAX_USER_SKILL_INVENTORY_ITEMS
        assert all(item["name"] != "official" for item in results)


# ============== read_sandbox_skill_content ==============

class TestReadSandboxSkillContent:
    """從沙箱讀取用戶 Skill 完整內容測試"""

    @pytest.mark.asyncio
    async def test_read_success(self, service, mock_sandbox):
        """測試成功讀取 Skill 內容（去除 frontmatter）"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.files.read_file = AsyncMock(
            return_value="---\nname: my-skill\ndescription: Test\n---\n## Usage\n\nDo something."
        )

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/my-skill"
        )

        assert content is not None
        assert "## Usage" in content
        assert "Do something." in content
        # frontmatter 已被去除
        assert "---" not in content
        assert "name: my-skill" not in content

    @pytest.mark.asyncio
    async def test_read_no_frontmatter(self, service, mock_sandbox):
        """測試沒有 frontmatter 的內容"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.files.read_file = AsyncMock(return_value="Just plain content")

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/plain"
        )

        assert content == "Just plain content"

    @pytest.mark.asyncio
    async def test_read_sandbox_not_cached(self, service):
        """測試沙箱不在快取中"""
        content = await service.read_sandbox_skill_content(
            "user-not-exist", "/home/user/skills/x"
        )
        assert content is None

    @pytest.mark.asyncio
    async def test_read_file_fails(self, service, mock_sandbox):
        """測試讀取失敗"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("not found"))

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/broken"
        )
        assert content is None

    @pytest.mark.asyncio
    async def test_read_bytes_content(self, service, mock_sandbox):
        """測試 read_file 返回 bytes 時自動解碼"""
        service._cache["user-1"] = mock_sandbox
        raw = "---\nname: b\ndescription: B\n---\n## Bytes Content".encode("utf-8")
        mock_sandbox.files.read_file = AsyncMock(return_value=raw)

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/bytes-skill"
        )

        assert content is not None
        assert "## Bytes Content" in content


# ============== _extract_skill_description_from_skill_md ==============

class TestExtractSkillDescription:
    """提取 Skill description 輔助方法測試"""

    def test_basic_extraction(self, service):
        text = "---\nname: foo\ndescription: A foo skill\n---\ncontent"
        result = service._extract_skill_description_from_skill_md(text)
        assert result == "A foo skill"

    def test_quoted_description(self, service):
        text = '---\nname: bar\ndescription: "Quoted desc"\n---\n'
        result = service._extract_skill_description_from_skill_md(text)
        assert result == "Quoted desc"

    def test_no_description(self, service):
        text = "---\nname: baz\n---\ncontent"
        result = service._extract_skill_description_from_skill_md(text)
        assert result is None

    def test_no_frontmatter(self, service):
        text = "Just content"
        result = service._extract_skill_description_from_skill_md(text)
        assert result is None


# ============== invalidate_cache ==============

class TestInvalidateCache:
    """快取失效測試"""

    def test_invalidate_existing(self, service, mock_sandbox):
        """測試清除已存在的快取"""
        service._cache["user-1"] = mock_sandbox
        service._pushed_skills["user-1"] = {"skill-a"}

        service.invalidate_cache("user-1")

        assert service.get_cached("user-1") is None
        assert "user-1" not in service._pushed_skills

    def test_invalidate_nonexistent(self, service):
        """測試清除不存在的快取（不報錯）"""
        service.invalidate_cache("nonexistent")
        assert service.get_cached("nonexistent") is None
