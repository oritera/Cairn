from __future__ import annotations

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.dispatch_config_store import config_path, load_redacted_document, validate_and_save
from cairn.server.event_store import insert_runtime_event
from cairn.server.models import DispatchConfigDocument, UpdateDispatchConfigRequest

router = APIRouter(tags=["dispatch-config"])


@router.get("/dispatch-config", response_model=DispatchConfigDocument)
def get_dispatch_config():
    path = config_path()
    if not path.exists():
        raise HTTPException(404, f"dispatch config not found: {path}")
    yaml_text, redacted, updated_at = load_redacted_document()
    return DispatchConfigDocument(path=str(path), yaml=yaml_text, redacted=redacted, updated_at=updated_at)


@router.put("/dispatch-config", response_model=DispatchConfigDocument)
def update_dispatch_config(body: UpdateDispatchConfigRequest):
    try:
        _config, restart_required = validate_and_save(body.yaml)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    with get_conn() as conn:
        insert_runtime_event(
            conn,
            project_id=None,
            event_type="config_updated",
            phase="dispatcher",
            status="warning" if restart_required else "success",
            message=(
                "Dispatch config saved; server/backend change requires dispatcher restart"
                if restart_required
                else "Dispatch config saved; dispatcher will hot-reload it"
            ),
            payload={"restart_required": restart_required},
        )
    yaml_text, redacted, updated_at = load_redacted_document()
    return DispatchConfigDocument(
        path=str(config_path()),
        yaml=yaml_text,
        redacted=redacted,
        updated_at=updated_at,
        restart_required=restart_required,
    )
