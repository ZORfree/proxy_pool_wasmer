"""
API module - provides proxy query, source management, and settings endpoints.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from .storage import get_storage

logger = logging.getLogger("proxy_pool.api")

router = APIRouter(prefix="/api", tags=["proxy"])


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------

class ProxyIn(BaseModel):
    ip: str
    port: int
    protocol: str = "http"
    anonymous: int = 0
    country: str = ""
    source: str = ""


class SourceIn(BaseModel):
    name: str = ""
    url: str
    type: str = "web"
    pattern: str = ""
    status: int = 1


class SettingsIn(BaseModel):
    settings: dict


# ---------------------------------------------------------------------------
# Proxy Endpoints
# ---------------------------------------------------------------------------

@router.get("/get")
def api_get_proxy(
    protocol: Optional[str] = Query(None, description="http / https / socks5"),
    country: Optional[str] = Query(None, description="Country code, e.g. US"),
    max_latency: Optional[float] = Query(None, description="Max latency in ms"),
    anonymous: Optional[bool] = Query(None, description="Require anonymous"),
):
    """Get a random proxy matching the filters."""
    filters = {}
    if protocol:
        filters["protocol"] = protocol
    if country:
        filters["country"] = country
    if max_latency is not None:
        filters["max_latency"] = max_latency
    if anonymous is not None:
        filters["anonymous"] = anonymous

    storage = get_storage()
    proxy = storage.get_random(filters)
    if not proxy:
        raise HTTPException(status_code=404, detail="No proxy available")
    return proxy


@router.get("/all")
def api_get_all(
    protocol: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    max_latency: Optional[float] = Query(None),
    anonymous: Optional[bool] = Query(None),
):
    """Get all proxies matching the filters."""
    filters = {}
    if protocol:
        filters["protocol"] = protocol
    if country:
        filters["country"] = country
    if max_latency is not None:
        filters["max_latency"] = max_latency
    if anonymous is not None:
        filters["anonymous"] = anonymous

    storage = get_storage()
    return storage.get_all(filters)


@router.get("/count")
def api_count(
    protocol: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
):
    """Count proxies matching the filters."""
    filters = {}
    if protocol:
        filters["protocol"] = protocol
    if country:
        filters["country"] = country

    storage = get_storage()
    return {"count": storage.get_count(filters)}


@router.get("/stats")
def api_stats():
    """Get aggregated proxy statistics."""
    storage = get_storage()
    return storage.get_stats()


@router.post("/proxy")
def api_add_proxy(proxy: ProxyIn):
    """Manually add a proxy."""
    storage = get_storage()
    ok = storage.add_proxy(proxy.model_dump())
    return {"success": ok}


@router.delete("/proxy")
def api_delete_proxy(
    ip: str = Query(...),
    port: int = Query(...),
    protocol: str = Query("http"),
):
    """Delete a proxy."""
    storage = get_storage()
    storage.delete_proxy(ip, port, protocol)
    return {"success": True}


# ---------------------------------------------------------------------------
# Source Endpoints
# ---------------------------------------------------------------------------

@router.get("/sources")
def api_get_sources(active_only: bool = Query(True)):
    """Get all proxy sources."""
    storage = get_storage()
    return storage.get_sources(active_only=active_only)


@router.post("/sources")
def api_add_source(source: SourceIn):
    """Add a new proxy source."""
    storage = get_storage()
    ok = storage.add_source(source.model_dump())
    return {"success": ok}


@router.put("/sources/{source_id}")
def api_update_source(source_id: int, source: SourceIn):
    """Update an existing source."""
    storage = get_storage()
    ok = storage.update_source(source_id, source.model_dump())
    return {"success": ok}


@router.delete("/sources/{source_id}")
def api_delete_source(source_id: int):
    """Delete a source."""
    storage = get_storage()
    storage.delete_source(source_id)
    return {"success": True}


# ---------------------------------------------------------------------------
# Settings Endpoints
# ---------------------------------------------------------------------------

@router.get("/settings")
def api_get_settings():
    """Get all settings."""
    storage = get_storage()
    return storage.get_all_settings()


@router.put("/settings")
def api_update_settings(body: SettingsIn):
    """Update multiple settings."""
    storage = get_storage()
    for k, v in body.settings.items():
        storage.set_setting(k, str(v))
    return {"success": True}


# ---------------------------------------------------------------------------
# Manual Triggers
# ---------------------------------------------------------------------------

@router.post("/fetch")
def api_trigger_fetch():
    """Manually trigger proxy fetching."""
    try:
        from .fetcher import run_fetch
        result = run_fetch()
        return {"success": True, "fetched": result}
    except ImportError:
        return {"success": False, "message": "Fetcher module not available yet."}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


@router.post("/validate")
def api_trigger_validate():
    """Manually trigger proxy validation."""
    try:
        from .validator import run_validate
        result = run_validate()
        return {"success": True, "validated": result}
    except ImportError:
        return {"success": False, "message": "Validator module not available yet."}
    except Exception as exc:
        return {"success": False, "message": str(exc)}
