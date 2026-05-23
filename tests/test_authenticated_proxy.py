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

    def test_add_proxy_upgrades_existing_endpoint_with_auth(self):
        storage = make_memory_storage(self)
        storage.add_proxy({"ip": "1.2.3.4", "port": 8080, "protocol": "http"})
        storage.add_proxy({
            "ip": "1.2.3.4",
            "port": 8080,
            "protocol": "http",
            "username": "alice",
            "password": "secret",
        })

        proxies = storage.get_all({"has_auth": True})

        self.assertEqual(1, len(proxies))
        self.assertEqual("alice", proxies[0]["username"])
        self.assertEqual("secret", proxies[0]["password"])

    def test_extract_proxies_keeps_url_auth(self):
        proxies = fetcher._extract_proxies(
            "http://alice:secret@1.2.3.4:8080\nsocks5://bob:s3cr3t@5.6.7.8:1080",
            protocol="auto",
        )

        self.assertIn(("1.2.3.4", 8080, "http", "alice", "secret"), proxies)
        self.assertIn(("5.6.7.8", 1080, "socks5", "bob", "s3cr3t"), proxies)

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


if __name__ == "__main__":
    unittest.main()
