"""
Storage module supporting both MySQL and SQLite.
If DB_* env vars are all present, uses MySQL (PyMySQL).
Otherwise, falls back to SQLite (sqlite3).
Manages proxies, sources, and global settings tables.
"""
import logging
import time
import contextlib
import os
from typing import Optional, List, Dict, Any

from .config import (
    USE_MYSQL, DB_HOST, DB_PORT, DB_NAME, DB_USERNAME, DB_PASSWORD,
    INITIAL_SCORE, MAX_SCORE, MIN_SCORE, SCORE_INCREMENT, SCORE_DECREMENT,
)
from .proxy_url import proxy_has_auth

logger = logging.getLogger("proxy_pool.storage")

DEFAULT_SETTINGS = {
    "validate_url": "https://api.ipapi.is",
    "validate_timeout": "10",
    "max_concurrency": "20",
    "fetch_interval": "300",
    "validate_interval": "600",
}

class Storage:
    def __init__(self):
        self._conn = None
        self.is_mysql = USE_MYSQL

        if self.is_mysql:
            import pymysql
            import pymysql.cursors
            self.pymysql = pymysql
            self.ph = "%s"
            self.ignore = "IGNORE"
            logger.info("Storage initialized with MySQL backend.")
        else:
            import sqlite3
            self.sqlite3 = sqlite3
            self.ph = "?"
            self.ignore = "OR IGNORE"
            logger.info("Storage initialized with SQLite backend.")

    def _get_conn(self):
        if self.is_mysql:
            if self._conn is None or not self._conn.open:
                self._conn = self.pymysql.connect(
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USERNAME,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    charset="utf8mb4",
                    cursorclass=self.pymysql.cursors.DictCursor,
                    autocommit=True,
                    connect_timeout=10,
                )
                logger.info("MySQL connected: %s@%s:%s/%s", DB_USERNAME, DB_HOST, DB_PORT, DB_NAME)
            else:
                try:
                    self._conn.ping(reconnect=True)
                except Exception:
                    # Connection might be dead, try to re-establish
                    self._conn = None
                    return self._get_conn()
        else:
            if self._conn is None:
                db_path = "/data/proxy_pool.db" if os.path.isdir("/data") else "proxy_pool.db"
                self._conn = self.sqlite3.connect(db_path, check_same_thread=False)
                self._conn.row_factory = self.sqlite3.Row
                self._conn.isolation_level = None  # autocommit mode
        return self._conn

    @contextlib.contextmanager
    def _get_cursor(self):
        conn = self._get_conn()
        if self.is_mysql:
            with conn.cursor() as cur:
                yield cur
        else:
            cur = conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def init(self):
        with self._get_cursor() as cur:
            if self.is_mysql:
                cur.execute("""
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
                        added_time  VARCHAR(32)  DEFAULT '',
                        source      VARCHAR(256) DEFAULT '',
                        username    VARCHAR(256) DEFAULT '',
                        `password`  VARCHAR(256) DEFAULT '',
                        UNIQUE KEY uq_proxy (ip, port, protocol)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sources (
                        id      INT AUTO_INCREMENT PRIMARY KEY,
                        name    VARCHAR(128) NOT NULL DEFAULT '',
                        url     VARCHAR(512) NOT NULL,
                        type    VARCHAR(16)  NOT NULL DEFAULT 'web',
                        pattern VARCHAR(512) DEFAULT '',
                        protocol VARCHAR(16) DEFAULT 'auto',
                        delimiter VARCHAR(16) DEFAULT 'newline',
                        status  TINYINT      DEFAULT 1,
                        UNIQUE KEY uq_url (url)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        `key`   VARCHAR(128) PRIMARY KEY,
                        `value` TEXT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
            else:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS proxies (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        ip          VARCHAR(64)  NOT NULL,
                        port        INTEGER      NOT NULL,
                        protocol    VARCHAR(16)  NOT NULL DEFAULT 'http',
                        anonymous   TINYINT      NOT NULL DEFAULT 0,
                        country     VARCHAR(8)   DEFAULT '',
                        latency     FLOAT        DEFAULT -1,
                        score       INTEGER      DEFAULT 50,
                        last_check  VARCHAR(32)  DEFAULT '',
                        added_time  VARCHAR(32)  DEFAULT '',
                        source      VARCHAR(256) DEFAULT '',
                        username    VARCHAR(256) DEFAULT '',
                        `password`  VARCHAR(256) DEFAULT '',
                        UNIQUE (ip, port, protocol)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sources (
                        id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        name    VARCHAR(128) NOT NULL DEFAULT '',
                        url     VARCHAR(512) NOT NULL,
                        type    VARCHAR(16)  NOT NULL DEFAULT 'web',
                        pattern VARCHAR(512) DEFAULT '',
                        protocol VARCHAR(16) DEFAULT 'auto',
                        delimiter VARCHAR(16) DEFAULT 'newline',
                        status  TINYINT      DEFAULT 1,
                        UNIQUE (url)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        `key`   VARCHAR(128) PRIMARY KEY,
                        `value` TEXT NOT NULL
                    );
                """)

            try:
                cur.execute("ALTER TABLE proxies ADD COLUMN added_time VARCHAR(32) DEFAULT ''")
            except Exception:
                pass  # Ignore if column already exists

            try:
                cur.execute("ALTER TABLE proxies ADD COLUMN username VARCHAR(256) DEFAULT ''")
            except Exception:
                pass

            try:
                cur.execute("ALTER TABLE proxies ADD COLUMN `password` VARCHAR(256) DEFAULT ''")
            except Exception:
                pass

            try:
                cur.execute("ALTER TABLE sources ADD COLUMN protocol VARCHAR(16) DEFAULT 'auto'")
                cur.execute("ALTER TABLE sources ADD COLUMN delimiter VARCHAR(16) DEFAULT 'newline'")
            except Exception:
                pass

            # Seed defaults
            for k, v in DEFAULT_SETTINGS.items():
                cur.execute(
                    f"INSERT {self.ignore} INTO settings (`key`, `value`) VALUES ({self.ph}, {self.ph})",
                    (k, v),
                )
        logger.info("Database tables initialized.")

    def close(self):
        if self._conn:
            if self.is_mysql and getattr(self._conn, "open", False):
                self._conn.close()
            elif not self.is_mysql:
                self._conn.close()
            logger.info("Database connection closed.")

    def add_proxy(self, proxy: Dict[str, Any]) -> bool:
        try:
            username = str(proxy.get("username") or "")
            password = str(proxy.get("password") or "")
            with self._get_cursor() as cur:
                cur.execute(
                    f"""INSERT {self.ignore} INTO proxies
                       (ip, port, protocol, anonymous, country,
                        latency, score, last_check, added_time, source,
                        username, `password`)
                       VALUES ({self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph})""",
                    (
                        proxy["ip"], proxy["port"],
                        proxy.get("protocol", "http"),
                        proxy.get("anonymous", 0),
                        proxy.get("country", ""),
                        proxy.get("latency", -1),
                        proxy.get("score", INITIAL_SCORE),
                        proxy.get("last_check", ""),
                        proxy.get("added_time", ""),
                        proxy.get("source", ""),
                        username,
                        password,
                    ),
                )
                if username or password:
                    cur.execute(
                        f"""UPDATE proxies SET username={self.ph}, `password`={self.ph}
                           WHERE ip={self.ph} AND port={self.ph} AND protocol={self.ph}""",
                        (
                            username,
                            password,
                            proxy["ip"], proxy["port"],
                            proxy.get("protocol", "http"),
                        ),
                    )
            return True
        except Exception as exc:
            logger.debug("add_proxy error: %s", exc)
            return False

    def update_proxy(self, proxy: Dict[str, Any]) -> bool:
        with self._get_cursor() as cur:
            sets = [
                f"anonymous={self.ph}",
                f"country={self.ph}",
                f"latency={self.ph}",
                f"score={self.ph}",
                f"last_check={self.ph}",
            ]
            values = [
                proxy.get("anonymous", 0),
                proxy.get("country", ""),
                proxy.get("latency", -1),
                proxy.get("score", 0),
                proxy.get("last_check", ""),
            ]
            if "username" in proxy or "password" in proxy:
                sets.extend([f"username={self.ph}", f"`password`={self.ph}"])
                values.extend([
                    str(proxy.get("username") or ""),
                    str(proxy.get("password") or ""),
                ])

            values.extend([proxy["ip"], proxy["port"], proxy.get("protocol", "http")])
            cur.execute(
                f"""UPDATE proxies SET {", ".join(sets)}
                   WHERE ip={self.ph} AND port={self.ph} AND protocol={self.ph}""",
                values,
            )
        return True

    def delete_proxy(self, ip: str, port: int, protocol: str = "http") -> bool:
        with self._get_cursor() as cur:
            cur.execute(
                f"DELETE FROM proxies WHERE ip={self.ph} AND port={self.ph} AND protocol={self.ph}",
                (ip, port, protocol),
            )
        return True

    def _row_to_proxy(self, row) -> Dict[str, Any]:
        proxy = dict(row)
        proxy.setdefault("username", "")
        proxy.setdefault("password", "")
        proxy["has_auth"] = proxy_has_auth(proxy)
        return proxy

    def get_random(self, filters: Optional[Dict] = None) -> Optional[Dict]:
        where, params = self._build_filter_clause(filters)
        rand_func = "RAND()" if self.is_mysql else "RANDOM()"
        with self._get_cursor() as cur:
            cur.execute(
                f"SELECT * FROM proxies {where} ORDER BY {rand_func} LIMIT 1", params
            )
            row = cur.fetchone()
            return self._row_to_proxy(row) if row else None

    def get_all(self, filters: Optional[Dict] = None) -> List[Dict]:
        where, params = self._build_filter_clause(filters)
        with self._get_cursor() as cur:
            cur.execute(
                f"SELECT * FROM proxies {where} ORDER BY score ASC, latency ASC",
                params,
            )
            rows = cur.fetchall()
            return [self._row_to_proxy(r) for r in rows] if rows else []

    def get_count(self, filters: Optional[Dict] = None) -> int:
        where, params = self._build_filter_clause(filters)
        with self._get_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM proxies {where}", params)
            row = cur.fetchone()
            if row:
                return dict(row)["cnt"]
            return 0

    def decrease_score(self, ip: str, port: int, protocol: str) -> None:
        func = "GREATEST" if self.is_mysql else "MAX"
        with self._get_cursor() as cur:
            cur.execute(
                f"UPDATE proxies SET score = {func}(score - {self.ph}, {self.ph}) WHERE ip={self.ph} AND port={self.ph} AND protocol={self.ph}",
                (SCORE_DECREMENT, MIN_SCORE, ip, port, protocol),
            )

    def increase_score(self, ip: str, port: int, protocol: str) -> None:
        func = "LEAST" if self.is_mysql else "MIN"
        with self._get_cursor() as cur:
            cur.execute(
                f"UPDATE proxies SET score = {func}(score + {self.ph}, {self.ph}) WHERE ip={self.ph} AND port={self.ph} AND protocol={self.ph}",
                (SCORE_INCREMENT, MAX_SCORE, ip, port, protocol),
            )

    def remove_low_score(self) -> int:
        with self._get_cursor() as cur:
            cur.execute(f"DELETE FROM proxies WHERE score <= {self.ph}", (MIN_SCORE,))
            return cur.rowcount

    def add_source(self, source: Dict[str, Any]) -> bool:
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"INSERT {self.ignore} INTO sources (name, url, type, pattern, protocol, delimiter, status) VALUES ({self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph})",
                    (
                        source.get("name", ""),
                        source["url"],
                        source.get("type", "web"),
                        source.get("pattern", ""),
                        source.get("protocol", "auto"),
                        source.get("delimiter", "newline"),
                        source.get("status", 1),
                    ),
                )
            return True
        except Exception:
            return False

    def get_sources(self, active_only: bool = True) -> List[Dict]:
        with self._get_cursor() as cur:
            if active_only:
                cur.execute("SELECT * FROM sources WHERE status=1")
            else:
                cur.execute("SELECT * FROM sources")
            rows = cur.fetchall()
            return [dict(r) for r in rows] if rows else []

    def update_source(self, source_id: int, data: Dict) -> bool:
        sets = ", ".join(f"`{k}`={self.ph}" if self.is_mysql else f"{k}={self.ph}" for k in data)
        vals = list(data.values()) + [source_id]
        with self._get_cursor() as cur:
            cur.execute(f"UPDATE sources SET {sets} WHERE id={self.ph}", vals)
        return True

    def delete_source(self, source_id: int) -> bool:
        with self._get_cursor() as cur:
            cur.execute(f"DELETE FROM sources WHERE id={self.ph}", (source_id,))
        return True

    def get_setting(self, key: str) -> Optional[str]:
        with self._get_cursor() as cur:
            cur.execute(f"SELECT `value` FROM settings WHERE `key`={self.ph}", (key,))
            row = cur.fetchone()
            if row:
                return dict(row)["value"]
            return None

    def set_setting(self, key: str, value: str) -> None:
        with self._get_cursor() as cur:
            if self.is_mysql:
                cur.execute(
                    f"INSERT INTO settings (`key`,`value`) VALUES ({self.ph},{self.ph}) "
                    f"ON DUPLICATE KEY UPDATE `value`={self.ph}",
                    (key, value, value),
                )
            else:
                cur.execute(
                    f"INSERT INTO settings (`key`,`value`) VALUES ({self.ph},{self.ph}) "
                    f"ON CONFLICT(`key`) DO UPDATE SET `value`={self.ph}",
                    (key, value, value),
                )

    def get_all_settings(self) -> Dict[str, str]:
        with self._get_cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM settings")
            rows = cur.fetchall()
            return {r["key"]: r["value"] for r in rows} if rows else {}

    def get_stats(self) -> Dict[str, Any]:
        with self._get_cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM proxies")
            total_row = cur.fetchone()
            total = dict(total_row).get("cnt", 0) if total_row else 0

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
                f"SELECT COUNT(*) as cnt FROM proxies WHERE score >= {self.ph}",
                (INITIAL_SCORE,),
            )
            active_row = cur.fetchone()
            active = dict(active_row).get("cnt", 0) if active_row else 0

        return {
            "total": total,
            "active": active,
            "by_country": {r["country"]: r["cnt"] for r in country_rows},
            "by_protocol": {r["protocol"]: r["cnt"] for r in proto_rows},
        }

    def _build_filter_clause(self, filters: Optional[Dict]) -> tuple:
        if not filters:
            return "", []
        clauses = []
        params = []
        for key, val in filters.items():
            if key == "max_latency" and val is not None:
                clauses.append(f"latency <= {self.ph} AND latency >= 0")
                params.append(float(val))
            elif key == "max_score" and val is not None:
                clauses.append(f"score <= {self.ph}")
                params.append(int(val))
            elif key == "anonymous" and val is not None:
                clauses.append(f"anonymous = {self.ph}")
                params.append(1 if val else 0)
            elif key == "has_auth" and val is not None:
                wants_auth = val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")
                if wants_auth:
                    clauses.append("(username != '' AND `password` != '')")
                else:
                    clauses.append("(username = '' OR `password` = '')")
            elif key == "protocol" and val:
                clauses.append(f"protocol = {self.ph}")
                params.append(str(val).lower())
            elif key == "country" and val:
                countries = [c.strip().upper() for c in str(val).split(',')]
                if len(countries) == 1:
                    clauses.append(f"country = {self.ph}")
                    params.append(countries[0])
                else:
                    placeholders = ', '.join([self.ph] * len(countries))
                    clauses.append(f"country IN ({placeholders})")
                    params.extend(countries)
        where = " AND ".join(clauses)
        return f"WHERE {where}" if where else "", params


_storage_instance: Optional[Storage] = None

def get_storage() -> Storage:
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance
    _storage_instance = Storage()
    _storage_instance.init()
    return _storage_instance
