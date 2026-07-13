"""Routes package — thin HTTP handlers that delegate to the service layer.

NOTE: The /metrics endpoint is added directly to the FastAPI app object in
app.py (not via api_router) so it bypasses JWT authentication.  In production
this endpoint MUST be firewall-restricted or protected by a reverse-proxy rule.
"""

from fastapi import APIRouter, Depends

from .control import router as control_router
from .files import router as files_router
from .sessions import router as sessions_router
from ..middleware.auth import verify_jwt

router = APIRouter(prefix="/api", dependencies=[Depends(verify_jwt)])
router.include_router(sessions_router)
router.include_router(files_router)
router.include_router(control_router)
