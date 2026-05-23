"""
Global configuration for Proxy Pool.
All settings are read from environment variables with sensible defaults.
"""
import os

# --- Database (Wasmer auto-provisions MySQL, else fallback to SQLite) ---
DB_MYSQL = os.getenv("DB_MYSQL")
USE_MYSQL = (DB_MYSQL == "True")

if USE_MYSQL:
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_NAME = os.getenv("DB_NAME", "proxy_pool")
    DB_USERNAME = os.getenv("DB_USERNAME", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
else:
    # Default values for local development when not using MySQL
    DB_HOST = "127.0.0.1"
    DB_PORT = 3306
    DB_NAME = "proxy_pool"
    DB_USERNAME = "root"
    DB_PASSWORD = ""

# --- Validation ---
VALIDATE_URL = os.getenv("VALIDATE_URL", "https://api.ipapi.is")
VALIDATE_TIMEOUT = int(os.getenv("VALIDATE_TIMEOUT", "10"))  # seconds
VALIDATE_FALLBACK_URLS = [
    url.strip()
    for url in os.getenv("VALIDATE_FALLBACK_URLS", "https://api.ipify.org?format=json").split(",")
    if url.strip()
]
MAX_VALIDATE_CONCURRENCY = int(os.getenv("MAX_VALIDATE_CONCURRENCY", "20"))

# --- Scoring ---
INITIAL_SCORE = 50
MAX_SCORE = 100
MIN_SCORE = 0
SCORE_INCREMENT = 1
SCORE_DECREMENT = 5

# --- Scheduler ---
FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL", "300"))  # seconds
VALIDATE_INTERVAL = int(os.getenv("VALIDATE_INTERVAL", "600"))  # seconds

# --- Server ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
