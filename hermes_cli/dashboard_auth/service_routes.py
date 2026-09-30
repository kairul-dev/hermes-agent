"""Read-only self inspection for an authenticated local service."""
from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/auth/service-capabilities")
async def service_capabilities(request: Request):
    return request.state.service_identity.capability()
