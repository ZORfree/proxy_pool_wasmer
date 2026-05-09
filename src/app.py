"""
FastAPI application instance with lifecycle management.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os

logger = logging.getLogger("proxy_pool")
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup/shutdown lifecycle: initialize and close DB."""
    logger.info("Proxy Pool starting up...")
    try:
        from .storage import get_storage
        storage = get_storage()
        logger.info("Database initialized successfully.")
    except Exception as exc:
        logger.warning("Database init failed (may not be available locally): %s", exc)
    yield
    # Shutdown
    try:
        from .storage import _storage_instance
        if _storage_instance:
            _storage_instance.close()
    except Exception:
        pass
    logger.info("Proxy Pool shut down.")


app = FastAPI(
    title="Proxy Pool",
    description="HTTP/SOCKS Proxy Pool on Wasmer Edge",
    lifespan=lifespan,
)

# Mount API routes
from .api import router as api_router
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
async def read_root():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    index_path = os.path.join(static_dir, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/hello")
async def hello():
    return {"message": "Hello from Proxy Pool on Wasmer Edge!", "status": "success"}


@app.get("/api/db-status")
def db_status():
    """Check database connectivity."""
    try:
        from .storage import get_storage
        storage = get_storage()
        count = storage.get_count()
        return {
            "status": "connected",
            "proxy_count": count,
            "message": "Database is operational.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }
