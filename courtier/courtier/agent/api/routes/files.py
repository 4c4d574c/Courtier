"""File upload route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, UploadFile

from ..middleware.auth import get_current_user
from ..rate_limiter import limiter

router = APIRouter()


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
    )
