"""
MySQL storage module using PyMySQL (pure Python, WASIX compatible).
Manages proxies, sources, and global settings tables.
"""
import logging
import time
from typing import Optional, List, Dict, Any

import pymysql
import pymysql.cursors

from .config import (
    DB_HOST, DB_PORT, DB_NAME, DB_USERNAME, DB_PASSWORD,
    INITIAL_SCORE, MAX_SCORE, MIN_SCORE, SCORE_INCREMENT, SCORE_DECREMENT,
)

logger = logging.getLogger("proxy_pool.storage")

# ---------------------------------------------------------------------------
# SQL Table Definitions
# ---------------------------------------------------------------------------

CREATE_PROXIES = """
CREATE TABLE IF NOT EXISTS proxies (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    ip          VARCHAR(64)  NOT NULL,
    port        INT          NOT NULL,
    protocol    VARCHAR(16)  NOT NULL DEFAULT 'http',
    anonymous   TINYINT      NOT NULL DEFAULT 0,
    country     VARCHAR(8)   DEFAULT '',
    latency     FLOAT        DEFAULT -1,
    score       INT          DEFAULT 50,
    last_check  VARCHAR(32)  DEFAULT '',
    source      VARCHAR(256) DEFAULT '',
    UNIQUE KEY uq_proxy (ip, port, protocol)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CREATE_SOURCES = """
CREATE TABLE IF NOT EXISTS sources (
    id      INT AUTO_INCREMENT PRIMARY KEY,
    name    VARCHAR(128) NOT NULL DEFAULT '',
    url     VARCHAR(512) NOT NULL,
    type    VARCHAR(16)  NOT NULL DEFAULT 'web',
    pattern VARCHAR(512) DEFAULT '',
    status  TINYINT      DEFAULT 1,
    UNIQUE KEY uq_url (url)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    `key`   VARCHAR(128) PRIMARY KEY,
    `value` TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DEFAULT_SETTINGS = {
    "validate_url": "https://api.ipapi.is",
    "validate_timeout": "10",
    "max_concurrency": "20",
    "fetch_interval": "300",
    "validate_interval": "600",
}


# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------

class Storage:
    """MySQL storage backend using PyMySQL."""

    def __init__(self):
        self._conn: Optional[pymysql.Connection] = None

    def _get_conn(self) -> pymysql.Connection:
        """Get or create a MySQL connection with auto-reconnect."""
        if self._conn is None or not self._conn.open:
            self._conn = pymysql.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USERNAME,
                password=DB_PASSWORD,
                database=DB_NAME,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=10,
            )
            logger.info("MySQL connected: %s@%s:%s/%s", DB_USERNAME, DB_HOST, DB_PORT, DB_NAME)
        return self._conn

    def init(self):
        """Initialize database tables and seed default settings."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(CREATE_PROXIES)
            cur.execute(CREATE_SOURCES)
            cur.execute(CREATE_SETTINGS)
            # Seed defaults
            for k, v in DEFAULT_SETTINGS.items():
                cur.execute(
                    "INSERT IGNORE INTO settings (`key`, `value`) VALUES (%s, %s)",
                    (k, v),
                )
        logger.info("Database tables initialized.")

    def close(self):
        """Close the MySQL connection."""
        if self._conn and self._conn.open:
            self._conn.close()
            logger.info("MySQL connection closed.")

    # --- Proxy operations ---

    def add_proxy(self, proxy: Dict[str, Any]) -> bool:
        """Insert a new proxy, ignoring duplicates."""
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT IGNORE INTO proxies
                       (ip, port, protocol, anonymous, country,
                        latency, score, last_check, source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        proxy["ip"], proxy["port"],
                        proxy.get("protocol", "http"),
                        proxy.get("anonymous", 0),
                        proxy.get("country", ""),
                        proxy.get("latency", -1),
                        proxy.get("score", INITIAL_SCORE),
                        proxy.get("last_check", ""),
                        proxy.get("source", ""),
                    ),
                )
            return True
        except Exception as exc:
            logger.debug("add_proxy error: %s", exc)
            return False

    def update_proxy(self, proxy: Dict[str, Any]) -> bool:
        """Update an existing proxy's attributes."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE proxies SET
                    anonymous=%s, country=%s,
                    latency=%s, score=%s, last_check=%s
                   WHERE ip=%s AND port=%s AND protocol=%s""",
                (
                    proxy.get("anonymous", 0),
                    proxy.get("country", ""),
                    proxy.get("latency", -1),
                    proxy.get("score", INITIAL_SCORE),
                    proxy.get("last_check", ""),
                    proxy["ip"], proxy["port"],
                    proxy.get("protocol", "http"),
                ),
            )
        return True

    def delete_proxy(self, ip: str, port: int, protocol: str = "http") -> bool:
        """Delete a proxy by its unique key."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM proxies WHERE ip=%s AND port=%s AND protocol=%s",
                (ip, port, protocol),
            )
        return True

    def get_random(self, filters: Optional[Dict] = None) -> Optional[Dict]:
        """Get a random proxy matching the given filters."""
        where, params = _build_filter_clause(filters)
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM proxies {where} ORDER BY RAND() LIMIT 1", params
            )
            return cur.fetchone()

    def get_all(self, filters: Optional[Dict] = None) -> List[Dict]:
        """Get all proxies matching the given filters."""
        where, params = _build_filter_clause(filters)
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM proxies {where} ORDER BY score DESC, latency ASC",
                params,
            )
            return cur.fetchall() or []

    def get_count(self, filters: Optional[Dict] = None) -> int:
        """Count proxies matching the given filters."""
        where, params = _build_filter_clause(filters)
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM proxies {where}", params)
            row = cur.fetchone()
            return row["cnt"] if row else 0

    def decrease_score(self, ip: str, port: int, protocol: str) -> None:
        """Decrease a proxy's score after a failed validation."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE proxies SET score = GREATEST(score - %s, %s) WHERE ip=%s AND port=%s AND protocol=%s",
                (SCORE_DECREMENT, MIN_SCORE, ip, port, protocol),
            )

    def increase_score(self, ip: str, port: int, protocol: str) -> None:
        """Increase a proxy's score after a successful validation."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE proxies SET score = LEAST(score + %s, %s) WHERE ip=%s AND port=%s AND protocol=%s",
                (SCORE_INCREMENT, MAX_SCORE, ip, port, protocol),
            )

    def remove_low_score(self) -> int:
        """Remove proxies with score at or below minimum."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM proxies WHERE score <= %s", (MIN_SCORE,))
            return cur.rowcount

    # --- Source operations ---

    def add_source(self, source: Dict[str, Any]) -> bool:
        """Add a new proxy source."""
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT IGNORE INTO sources (name, url, type, pattern, status) VALUES (%s,%s,%s,%s,%s)",
                    (
                        source.get("name", ""),
                        source["url"],
                        source.get("type", "web"),
                        source.get("pattern", ""),
                        source.get("status", 1),
                    ),
                )
            return True
        except Exception:
            return False

    def get_sources(self, active_only: bool = True) -> List[Dict]:
        """Get all proxy sources."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            if active_only:
                cur.execute("SELECT * FROM sources WHERE status=1")
            else:
                cur.execute("SELECT * FROM sources")
            return cur.fetchall() or []

    def update_source(self, source_id: int, data: Dict) -> bool:
        """Update a source by ID."""
        sets = ", ".join(f"`{k}`=%s" for k in data)
        vals = list(data.values()) + [source_id]
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"UPDATE sources SET {sets} WHERE id=%s", vals)
        return True

    def delete_source(self, source_id: int) -> bool:
        """Delete a source by ID."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sources WHERE id=%s", (source_id,))
        return True

    # --- Settings ---

    def get_setting(self, key: str) -> Optional[str]:
        """Get a single setting value."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT `value` FROM settings WHERE `key`=%s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value (insert or update)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (`key`,`value`) VALUES (%s,%s) "
                "ON DUPLICATE KEY UPDATE `value`=%s",
                (key, value, value),
            )

    def get_all_settings(self) -> Dict[str, str]:
        """Get all settings as a dict."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM settings")
            rows = cur.fetchall() or []
            return {r["key"]: r["value"] for r in rows}

    # --- Stats ---

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregated statistics."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM proxies")
            total = (cur.fetchone() or {}).get("cnt", 0)

            cur.execute(
                "SELECT country, COUNT(*) as cnt FROM proxies "
                "WHERE country != '' GROUP BY country ORDER BY cnt DESC"
            )
            country_rows = cur.fetchall() or []

            cur.execute(
                "SELECT protocol, COUNT(*) as cnt FROM proxies "
                "GROUP BY protocol ORDER BY cnt DESC"
            )
            proto_rows = cur.fetchall() or []

            cur.execute(
                "SELECT COUNT(*) as cnt FROM proxies WHERE score >= %s",
                (INITIAL_SCORE,),
            )
            active = (cur.fetchone() or {}).get("cnt", 0)

        return {
            "total": total,
            "active": active,
            "by_country": {r["country"]: r["cnt"] for r in country_rows},
            "by_protocol": {r["protocol"]: r["cnt"] for r in proto_rows},
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _build_filter_clause(filters: Optional[Dict]) -> tuple:
    """Build SQL WHERE clause from filter dict."""
    if not filters:
        return "", []
    clauses = []
    params = []
    for key, val in filters.items():
        if key == "max_latency" and val is not None:
            clauses.append("latency <= %s AND latency >= 0")
            params.append(float(val))
        elif key == "anonymous" and val is not None:
            clauses.append("anonymous = %s")
            params.append(1 if val else 0)
        elif key == "protocol" and val:
            clauses.append("protocol = %s")
            params.append(str(val).lower())
        elif key == "country" and val:
            clauses.append("country = %s")
            params.append(str(val).upper())
    where = " AND ".join(clauses)
    return f"WHERE {where}" if where else "", params


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_storage_instance: Optional[Storage] = None


def get_storage() -> Storage:
    """Return the singleton storage instance, creating it on first call."""
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance
    _storage_instance = Storage()
    _storage_instance.init()
    return _storage_instance
