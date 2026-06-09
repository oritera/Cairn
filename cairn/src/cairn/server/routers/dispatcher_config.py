from __future__ import annotations

from fastapi import APIRouter

from cairn.server.dispatcher_config import build_dispatcher_config_response, update_dispatcher_config
from cairn.server.models import DispatcherConfigResponse, UpdateDispatcherConfigRequest

router = APIRouter(tags=["dispatcher-config"])


@router.get("/dispatcher-config", response_model=DispatcherConfigResponse)
def get_dispatcher_config():
    path, workers = build_dispatcher_config_response()
    return DispatcherConfigResponse(config_path=str(path), workers=workers)


@router.put("/dispatcher-config", response_model=DispatcherConfigResponse)
def save_dispatcher_config(body: UpdateDispatcherConfigRequest):
    path, workers = update_dispatcher_config(body.workers)
    return DispatcherConfigResponse(config_path=str(path), workers=workers)
