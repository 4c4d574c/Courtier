"""File upload/download routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from fastapi import UploadFile

from ..middleware.auth import get_current_user
from ..rate_limiter import limiter
from ..services.file_service import ALLOWED_MIME_TYPES

router = APIRouter()


def _media_type_for(name: str) -> str:
    """First registered MIME for the extension; downloads fall back to octet-stream."""
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mimes = ALLOWED_MIME_TYPES.get(ext)
    return next(iter(mimes)) if mimes else "application/octet-stream"


@router.post("/files")
@limiter.limit("10/minute")
async def upload_file(
    request: Request,
    file: UploadFile,
    current_user_payload: dict = Depends(get_current_user),
):
    """Upload a document file, return a fileId for use in session creation."""
    from ..services.file_service import upload_file as upload_file_service

    settings = request.app.state.settings
    file_store = request.app.state.file_store

    return await upload_file_service(
        file,
        settings,
        file_store,
        owner=current_user_payload.get("sub", ""),
        tool_registry=getattr(request.app.state, "tool_registry", None),
    )


@router.get("/files/{file_id}")
@limiter.limit("60/minute")
async def download_file(
    request: Request,
    file_id: str,
    current_user_payload: dict = Depends(get_current_user),
):
    """Serve a previously uploaded file (inline preview / download).

    Authentication relies on the access_token cookie for <iframe> and
    <a download> consumers, which cannot send an Authorization header.
    """
    settings = request.app.state.settings
    file_store = request.app.state.file_store

    path = await file_store.authorized_path(
        file_id,
        settings.upload_dir,
        current_user_payload.get("sub", ""),
        current_user_payload.get("role") == "admin",
    )
    info = await file_store.resolve(file_id)
    name = info.original_name if info else file_id
    # Serve by the STORED file's extension: transcoded videos keep their
    # original display name but the bytes are mp4 after normalization.
    media_type = _media_type_for(Path(path).name) or _media_type_for(name)
    return FileResponse(
        path,
        media_type=media_type,
        filename=name,
        content_disposition_type="inline",
    )
