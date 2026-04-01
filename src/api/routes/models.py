"""模型列表 API — 提供前端安全的模型配置查詢"""
import logging
from fastapi import APIRouter, HTTPException
from src.api.model_registry import get_model_registry

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_models():
    """列出所有可用模型（不含敏感字段）

    Returns:
        {
            "models": [{"id", "name", "provider", "supports_thinking", "max_tokens", "tags"}],
            "default_model": "minimax-text-01"
        }
    """
    try:
        registry = get_model_registry()
        return {
            "models": registry.list_public(),
            "default_model": registry.default_model_id,
        }
    except Exception as e:
        logger.error("加載模型配置失敗: %s", e)
        raise HTTPException(status_code=500, detail=f"模型配置加載失敗: {str(e)}")


@router.get("/{model_id}")
async def get_model(model_id: str):
    """查詢單個模型信息

    Args:
        model_id: 模型 ID

    Returns:
        模型公開信息
    """
    registry = get_model_registry()
    config = registry.get(model_id)
    if config is None or not config.enabled:
        available = [m.id for m in registry.list_models(enabled_only=True)]
        raise HTTPException(
            status_code=404,
            detail=f"模型 '{model_id}' 不存在或已停用。可用模型: {available}"
        )
    return config.to_public_dict()
