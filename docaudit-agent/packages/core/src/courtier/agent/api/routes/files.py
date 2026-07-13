"""File upload route."""

from __future__ import annotations

from fastapi import APIRouter, Request, UploadFile

router = APIRouter()


@router.post("/files")
async def upload_file(request: Request, file: UploadFile):
    """Upload a document file, return a fileId for use in session creation."""
    from ..services.file_service import upload_file as upload_file_service

    settings = request.app.state.settings
    file_store = request.app.state.file_store

    return await upload_file_service(file, settings, file_store)
