"""模型列表 API — 提供前端安全的模型配置查詢"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.api.models.database import get_db
from src.api.model_registry import get_model_registry
from src.api.services.model_access_service import (
    assert_user_can_access_model,
    list_accessible_model_configs,
    resolve_default_model_for_user,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_models(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """列出当前用户可用模型（不含敏感字段）

    Returns:
        {
            "models": [{"id", "name", "provider", "supports_thinking", "max_tokens", "tags"}],
            "default_model": "minimax-text-01",
            "subagent_default_model": "minimax-text-01"
        }
    """
    try:
        registry = get_model_registry()
        models = list_accessible_model_configs(db, user_id, registry)
        default_model = resolve_default_model_for_user(db, user_id, registry=registry)
        subagent_default_model = resolve_default_model_for_user(
            db,
            user_id,
            kind="subagent",
            registry=registry,
        )
        return {
            "models": [model.to_public_dict() for model in models],
            "default_model": default_model.id,
            "subagent_default_model": subagent_default_model.id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("加載模型配置失敗: %s", e)
        raise HTTPException(status_code=500, detail=f"模型配置加載失敗: {str(e)}")


@router.get("/{model_id}")
async def get_model(
    model_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """查詢当前用户可访问的單個模型信息

    Args:
        model_id: 模型 ID

    Returns:
        模型公開信息
    """
    registry = get_model_registry()
    config = assert_user_can_access_model(db, user_id, model_id, registry)
    return config.to_public_dict()
