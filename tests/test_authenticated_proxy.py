import asyncio
import sqlite3
import unittest

from fastapi.testclient import TestClient

import src.storage as storage_module
from src import fetcher, validator
from src.app import app
from src.storage import Storage


def make_memory_storage(test_case=None):
    storage = Storage()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    storage._conn = conn
    storage.init()
    if test_case is not None:
        test_case.addCleanup(storage.close)
    return storage


class AuthenticatedProxyTests(unittest.TestCase):
    def test_storage_filters_by_auth_presence(self):
        storage = make_memory_storage(self)
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
        })
        storage.add_proxy({
            "ip": "5.6.7.8",
            "port": 3128,
            "protocol": "http",
        })

        with_auth = storage.get_all({"has_auth": True})
        without_auth = storage.get_all({"has_auth": False})

        self.assertEqual(["1.2.3.4"], [p["ip"] for p in with_auth])
        self.assertTrue(with_auth[0]["has_auth"])
        self.assertEqual(["5.6.7.8"], [p["ip"] for p in without_auth])
        self.assertFalse(without_auth[0]["has_auth"])

    def test_sqlite_init_migrates_legacy_unique_key(self):
        storage = Storage()
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        storage._conn = conn
        self.addCleanup(storage.close)

        conn.execute("""
            CREATE TABLE proxies (
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
                UNIQUE (ip, port, protocol)
            );
        """)
        conn.execute(
            "INSERT INTO proxies (ip, port, protocol) VALUES (?, ?, ?)",
            ("1.2.3.4", 8080, "http"),
        )

        storage.init()
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
        })
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "bob",
            "password": "secret",
        })

        self.assertEqual(3, len(storage.get_all()))

    def test_add_proxy_keeps_distinct_credentials_for_same_endpoint(self):
        storage = make_memory_storage(self)
        storage.add_proxy({"ip": "1.2.3.4", "port": 8080, "protocol": "http"})
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
        })
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "bob",
            "password": "secret",
        })

        proxies = storage.get_all()
        identities = {
            (p["ip"], p["port"], p["protocol"], p["username"], p["password"])
            for p in proxies
        }

        self.assertEqual(3, len(proxies))
        self.assertIn(("1.2.3.4", 8080, "http", "", ""), identities)
        self.assertIn(("1.2.3.4", 8080, "http", "alice", "secret"), identities)
        self.assertIn(("1.2.3.4", 8080, "http", "bob", "secret"), identities)

    def test_extract_proxies_keeps_url_auth(self):
        proxies = fetcher._extract_proxies(
            "http://alice:secret@1.2.3.4:8080\nsocks5://bob:s3cr3t@5.6.7.8:1080",
            protocol="auto",
        )

        self.assertIn(("1.2.3.4", 8080, "http", "alice", "secret"), proxies)
        self.assertIn(("5.6.7.8", 1080, "socks5", "bob", "s3cr3t"), proxies)

    def test_fetch_dedupe_keeps_distinct_credentials_for_same_endpoint(self):
        seen_keys = set()
        raw_proxies = []
        source_proxies = [
            {
                "ip": "1.2.3.4",
                "port": 8080,
                "protocol": "http",
                "username": "alice",
                "password": "secret",
            },
            {
                "ip": "1.2.3.4",
                "port": 8080,
                "protocol": "http",
                "username": "bob",
                "password": "secret",
            },
        ]

        for proxy in source_proxies:
            key = fetcher._proxy_identity_key(proxy)
            if key not in seen_keys:
                seen_keys.add(key)
                raw_proxies.append(proxy)

        self.assertEqual(2, len(raw_proxies))

    def test_update_proxy_targets_matching_credentials_only(self):
        storage = make_memory_storage(self)
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
            "latency": 100,
        })
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "bob",
            "password": "secret",
            "latency": 200,
        })

        storage.update_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
            "latency": 50,
            "score": 10,
        })

        by_user = {p["username"]: p for p in storage.get_all()}
        self.assertEqual(50, by_user["alice"]["latency"])
        self.assertEqual(200, by_user["bob"]["latency"])

    def test_delete_proxy_targets_matching_credentials_only(self):
        storage = make_memory_storage(self)
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
        })
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "bob",
            "password": "secret",
        })

        storage.delete_proxy("1.2.3.4", 8080, "http", "alice", "secret")

        proxies = storage.get_all()
        self.assertEqual(1, len(proxies))
        self.assertEqual("bob", proxies[0]["username"])

    def test_validate_single_builds_authenticated_proxy_url(self):
        captured_urls = []

        async def fake_request(proxy_url, target_url, timeout):
            captured_urls.append(proxy_url)
            return {"location": {"country_code": "US"}}

        original = validator._request_via_proxy
        validator._request_via_proxy = fake_request
        try:
            result = asyncio.run(validator._validate_single(
                {
                    "ip": "1.2.3.4",
                    "port": 8080,
                    "protocol": "http",
                    "username": "alice",
                    "password": "secret",
                },
                "https://example.test",
                5,
                asyncio.Semaphore(1),
            ))
        finally:
            validator._request_via_proxy = original

        self.assertTrue(result["valid"])
        self.assertEqual(["http://alice:secret@1.2.3.4:8080"], captured_urls)

    def test_validate_single_uses_fallback_url_when_primary_fails(self):
        captured_targets = []

        async def fake_request(proxy_url, target_url, timeout):
            captured_targets.append(target_url)
            if target_url == "https://api.ipapi.is":
                return None
            return {"ip": "1.2.3.4"}

        original_request = validator._request_via_proxy
        original_fallbacks = getattr(validator, "VALIDATE_FALLBACK_URLS", None)
        validator._request_via_proxy = fake_request
        validator.VALIDATE_FALLBACK_URLS = ("https://api.ipify.org?format=json",)
        try:
            result = asyncio.run(validator._validate_single(
                {
                    "ip": "1.2.3.4",
                    "port": 8080,
                    "protocol": "socks5",
                    "username": "alice",
                    "password": "secret",
                },
                "https://api.ipapi.is",
                5,
                asyncio.Semaphore(1),
            ))
        finally:
            validator._request_via_proxy = original_request
            if original_fallbacks is None:
                delattr(validator, "VALIDATE_FALLBACK_URLS")
            else:
                validator.VALIDATE_FALLBACK_URLS = original_fallbacks

        self.assertTrue(result["valid"])
        self.assertEqual(
            ["https://api.ipapi.is", "https://api.ipify.org?format=json"],
            captured_targets,
        )

    def test_fetch_validate_proxy_uses_fallback_url_when_primary_fails(self):
        captured_targets = []

        async def fake_request(proxy_url, target_url, timeout):
            captured_targets.append(target_url)
            if target_url == "https://api.ipapi.is":
                return None
            return {"ip": "1.2.3.4"}

        original_request = fetcher._request_via_proxy
        original_fallbacks = getattr(fetcher, "VALIDATE_FALLBACK_URLS", None)
        fetcher._request_via_proxy = fake_request
        fetcher.VALIDATE_FALLBACK_URLS = ("https://api.ipify.org?format=json",)
        try:
            result = asyncio.run(fetcher._validate_proxy(
                {
                    "ip": "1.2.3.4",
                    "port": 8080,
                    "protocol": "socks5",
                    "username": "alice",
                    "password": "secret",
                },
                "https://api.ipapi.is",
                5,
                asyncio.Semaphore(1),
            ))
        finally:
            fetcher._request_via_proxy = original_request
            if original_fallbacks is None:
                delattr(fetcher, "VALIDATE_FALLBACK_URLS")
            else:
                fetcher.VALIDATE_FALLBACK_URLS = original_fallbacks

        self.assertTrue(result["valid"])
        self.assertEqual(
            ["https://api.ipapi.is", "https://api.ipify.org?format=json"],
            captured_targets,
        )

    def test_api_filters_and_simple_output_include_auth(self):
        storage = make_memory_storage()
        storage_module._storage_instance = storage
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
        })
        storage.add_proxy({"ip": "5.6.7.8", "port": 3128, "protocol": "http"})

        try:
            with TestClient(app) as client:
                with_auth = client.get("/api/all?has_auth=true").json()
                without_auth = client.get("/api/all?has_auth=false").json()
                simple = client.get("/api/simple?has_auth=true").text
                count = client.get("/api/count?has_auth=true").json()
        finally:
            storage_module._storage_instance = None

        self.assertEqual(["1.2.3.4"], [p["ip"] for p in with_auth])
        self.assertEqual(["5.6.7.8"], [p["ip"] for p in without_auth])
        self.assertEqual("http://alice:secret@1.2.3.4:8080", simple)
        self.assertEqual({"count": 1}, count)

    def test_api_filters_by_source_across_proxy_endpoints(self):
        storage = make_memory_storage()
        storage_module._storage_instance = storage
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "source": "手动",
        })
        storage.add_proxy({
            "ip": "5.6.7.8",
            "port": 3128,
            "protocol": "http",
            "source": "source-a",
        })

        try:
            with TestClient(app) as client:
                params = {"source": "手动"}
                all_result = client.get("/api/all", params=params).json()
                random_result = client.get("/api/random", params=params).json()
                simple = client.get("/api/simple", params=params).text
                count = client.get("/api/count", params=params).json()
        finally:
            storage_module._storage_instance = None

        self.assertEqual(["1.2.3.4"], [p["ip"] for p in all_result])
        self.assertEqual("1.2.3.4", random_result["ip"])
        self.assertEqual("http://1.2.3.4:8080", simple)
        self.assertEqual({"count": 1}, count)

    def test_api_stats_groups_protocol_counts_by_source(self):
        storage = make_memory_storage()
        storage_module._storage_instance = storage
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "source": "手动",
        })
        storage.add_proxy({
            "ip": "5.6.7.8",
            "port": 1080,
            "protocol": "socks5",
            "source": "手动",
        })
        storage.add_proxy({
            "ip": "9.9.9.9",
            "port": 3128,
            "protocol": "http",
            "source": "source-a",
        })

        try:
            with TestClient(app) as client:
                stats = client.get("/api/stats").json()
        finally:
            storage_module._storage_instance = None

        self.assertEqual({
            "手动": {"http": 1, "socks5": 1},
            "source-a": {"http": 1},
        }, stats["by_source_protocol"])

    def test_api_delete_targets_matching_credentials_only(self):
        storage = make_memory_storage()
        storage_module._storage_instance = storage
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
        })
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "bob",
            "password": "secret",
        })

        try:
            with TestClient(app) as client:
                response = client.delete(
                    "/api/proxy",
                    params={
                        "ip": "1.2.3.4",
                        "port": 8080,
                        "protocol": "http",
                        "username": "alice",
                        "password": "secret",
                    },
                )
                remaining = client.get("/api/all?has_auth=true").json()
        finally:
            storage_module._storage_instance = None

        self.assertEqual(200, response.status_code)
        self.assertEqual(["bob"], [p["username"] for p in remaining])

    def test_api_add_proxy_defaults_manual_source(self):
        storage = make_memory_storage()
        storage_module._storage_instance = storage

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/proxy",
                    json={"ip": "1.2.3.4", "port": 8080, "protocol": "http"},
                )
                proxies = client.get("/api/all").json()
        finally:
            storage_module._storage_instance = None

        self.assertEqual(200, response.status_code)
        self.assertEqual("手动", proxies[0]["source"])

    def test_api_batch_add_defaults_manual_source(self):
        storage = make_memory_storage()
        storage_module._storage_instance = storage

        async def fake_request(proxy_url, target_url, timeout):
            return {"location": {"country_code": "US"}}

        original_request = validator._request_via_proxy
        validator._request_via_proxy = fake_request
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/batch-add",
                    json={
                        "proxies": [
                            {"ip": "1.2.3.4", "port": 8080, "protocol": "http"}
                        ]
                    },
                )
                proxies = client.get("/api/all").json()
        finally:
            validator._request_via_proxy = original_request
            storage_module._storage_instance = None

        self.assertEqual(200, response.status_code)
        self.assertEqual("手动", proxies[0]["source"])

    def test_api_fetch_single_source_passes_source_id_to_fetcher(self):
        storage = make_memory_storage()
        storage_module._storage_instance = storage
        storage.add_source({
            "name": "source-a",
            "url": "https://example.test/a.txt",
            "type": "api",
            "protocol": "http",
            "delimiter": "newline",
            "status": 1,
        })
        source_id = storage.get_sources(active_only=False)[0]["id"]

        captured_source_ids = []
        original_run_fetch = fetcher.run_fetch

        def fake_run_fetch(source_id=None):
            captured_source_ids.append(source_id)
            return {"sources_crawled": 1, "proxies_found": 0, "validated": 0, "stored": 0}

        fetcher.run_fetch = fake_run_fetch
        try:
            with TestClient(app) as client:
                response = client.get(f"/api/sources/{source_id}/fetch")
        finally:
            fetcher.run_fetch = original_run_fetch
            storage_module._storage_instance = None

        self.assertEqual(200, response.status_code)
        self.assertEqual([source_id], captured_source_ids)


if __name__ == "__main__":
    unittest.main()
