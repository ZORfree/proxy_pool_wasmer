"""
API module - provides proxy query, source management, and settings endpoints.
"""
import logging
import threading
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .storage import get_storage
from .proxy_url import build_proxy_url

logger = logging.getLogger("proxy_pool.api")

router = APIRouter(prefix="/api", tags=["proxy"])


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------

class ProxyIn(BaseModel):
    ip: str
    port: int
    protocol: str = "http"
    username: str = ""
    password: str = ""
    anonymous: int = 0
    country: str = ""
    source: str = ""


class ProxyBatchIn(BaseModel):
    proxies: list[dict]  # list of {ip, port, protocol}


class SourceIn(BaseModel):
    name: str = ""
    url: str
    type: str = "web"
    pattern: str = ""
    protocol: str = "auto"
    delimiter: str = "newline"
    status: int = 1


class SettingsIn(BaseModel):
    settings: dict


# ---------------------------------------------------------------------------
# Proxy Endpoints
# ---------------------------------------------------------------------------

def _build_proxy_filters(
    protocol: Optional[str] = None,
    country: Optional[str] = None,
    max_latency: Optional[float] = None,
    max_score: Optional[int] = None,
    anonymous: Optional[bool] = None,
    has_auth: Optional[bool] = None,
) -> dict:
    filters = {}
    if protocol:
        filters["protocol"] = protocol
    if country:
        filters["country"] = country
    if max_latency is not None:
        filters["max_latency"] = max_latency
    if max_score is not None:
        filters["max_score"] = max_score
    if anonymous is not None:
        filters["anonymous"] = anonymous
    if has_auth is not None:
        filters["has_auth"] = has_auth
    return filters


@router.get("/random")
def api_get_proxy(
    protocol: Optional[str] = Query(None, description="http / https / socks5"),
    country: Optional[str] = Query(None, description="Country code, e.g. US,CN"),
    max_latency: Optional[float] = Query(None, description="Max latency in ms"),
    max_score: Optional[int] = Query(None, description="Max risk score"),
    anonymous: Optional[bool] = Query(None, description="Require anonymous"),
    has_auth: Optional[bool] = Query(None, description="Require proxy authentication info"),
):
    """Get a random proxy matching the filters."""
    filters = _build_proxy_filters(protocol, country, max_latency, max_score, anonymous, has_auth)

    storage = get_storage()
    proxy = storage.get_random(filters)
    if not proxy:
        raise HTTPException(status_code=404, detail="No proxy available")
    return proxy


@router.get("/all")
def api_get_all(
    protocol: Optional[str] = Query(None),
    country: Optional[str] = Query(None, description="Country code, e.g. US,CN"),
    max_latency: Optional[float] = Query(None),
    max_score: Optional[int] = Query(None),
    anonymous: Optional[bool] = Query(None),
    has_auth: Optional[bool] = Query(None),
):
    """Get all proxies matching the filters."""
    filters = _build_proxy_filters(protocol, country, max_latency, max_score, anonymous, has_auth)

    storage = get_storage()
    return storage.get_all(filters)


@router.get("/simple", response_class=PlainTextResponse)
def api_get_simple(
    protocol: Optional[str] = Query(None, description="http / https / socks5"),
    country: Optional[str] = Query(None, description="Country code, e.g. US,CN"),
    max_latency: Optional[float] = Query(None, description="Max latency in ms"),
    max_score: Optional[int] = Query(None, description="Max risk score"),
    anonymous: Optional[bool] = Query(None, description="Require anonymous"),
    has_auth: Optional[bool] = Query(None, description="Require proxy authentication info"),
):
    """Get all proxies matching the filters in plain text format."""
    filters = _build_proxy_filters(protocol, country, max_latency, max_score, anonymous, has_auth)

    storage = get_storage()
    proxies = storage.get_all(filters)
    lines = [build_proxy_url(p) for p in proxies]
    return "\n".join(lines)


@router.get("/count")
def api_count(
    protocol: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    max_latency: Optional[float] = Query(None),
    max_score: Optional[int] = Query(None),
    anonymous: Optional[bool] = Query(None),
    has_auth: Optional[bool] = Query(None),
):
    """Count proxies matching the filters."""
    filters = _build_proxy_filters(protocol, country, max_latency, max_score, anonymous, has_auth)

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


@router.post("/batch-add")
async def api_batch_add(body: ProxyBatchIn):
    """Batch add proxies, instantly validate them, and only save valid ones."""
    from .validator import async_validate_and_add
    
    result = await async_validate_and_add(body.proxies)
    return {"success": True, "data": result}


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


@router.post("/batch-delete")
def api_batch_delete(body: ProxyBatchIn):
    """Batch delete proxies."""
    storage = get_storage()
    count = 0
    for p in body.proxies:
        if storage.delete_proxy(p['ip'], p['port'], p.get('protocol', 'http')):
            count += 1
    return {"success": True, "count": count}


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

_fetch_lock = threading.Lock()
_validate_lock = threading.Lock()

def _run_fetch_task():
    if not _fetch_lock.acquire(blocking=False):
        return
    try:
        from .fetcher import run_fetch
        run_fetch()
    except Exception as e:
        logger.error("Fetch task error: %s", e)
    finally:
        _fetch_lock.release()

def _run_validate_task():
    if not _validate_lock.acquire(blocking=False):
        return
    try:
        from .validator import run_validate
        run_validate()
    except Exception as e:
        logger.error("Validate task error: %s", e)
    finally:
        _validate_lock.release()

@router.get("/fetch")
def api_trigger_fetch(background_tasks: BackgroundTasks):
    """Trigger proxy fetching in background."""
    if _fetch_lock.locked():
        return {"success": False, "message": "Fetch task is already running."}
    background_tasks.add_task(_run_fetch_task)
    return {"success": True, "message": "Fetch task started in background."}


@router.get("/check")
def api_trigger_check(background_tasks: BackgroundTasks):
    """Trigger proxy validation in background."""
    if _validate_lock.locked():
        return {"success": False, "message": "Check task is already running."}
    background_tasks.add_task(_run_validate_task)
    return {"success": True, "message": "Check task started in background."}


def _run_batch_validate_task(proxies: list):
    if not _validate_lock.acquire(blocking=False):
        return
    try:
        from .validator import run_validate
        run_validate(target_proxies=proxies)
    except Exception as e:
        logger.error("Batch validate task error: %s", e)
    finally:
        _validate_lock.release()


@router.post("/batch-check")
def api_batch_check(body: ProxyBatchIn, background_tasks: BackgroundTasks):
    """Batch check specific proxies."""
    if _validate_lock.locked():
        return {"success": False, "message": "A validation task is already running."}
    background_tasks.add_task(_run_batch_validate_task, body.proxies)
    return {"success": True, "message": f"Started background check for {len(body.proxies)} proxies."}
