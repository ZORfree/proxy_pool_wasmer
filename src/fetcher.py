"""
Fetcher module - crawls public proxy sources, validates each proxy,
and only stores validated proxies to the database.

Flow: Fetch IP:Port list -> Validate via ipapi.is -> Store valid ones only.
Uses aiohttp and asyncio for high performance.
"""
import logging
import re
import time
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Set, Tuple, Optional

import aiohttp
from aiohttp_socks import ProxyConnector

from .storage import get_storage
from .config import VALIDATE_URL, VALIDATE_TIMEOUT, VALIDATE_FALLBACK_URLS, MAX_VALIDATE_CONCURRENCY
from .score import calculate_risk_score
from .proxy_url import build_proxy_url, parse_proxy_line, proxy_identity_key

logger = logging.getLogger("proxy_pool.fetcher")

BUILTIN_SOURCES = []

IP_PORT_PATTERN = re.compile(
    r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})"
)

def _proxy_identity_key(proxy: Dict) -> tuple:
    return proxy_identity_key(proxy)

def _detect_protocol_from_url(url: str) -> str:
    url_lower = url.lower()
    if "socks5" in url_lower or "socks" in url_lower:
        return "socks5"
    if "socks4" in url_lower:
        return "socks4"
    return "http"

async def _fetch_url(session: aiohttp.ClientSession, url: str, timeout: int = 15) -> str:
    try:
        logger.debug("[HTTP] GET %s (timeout=%ds)", url, timeout)
        async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
            resp.raise_for_status()
            text = await resp.text()
            logger.debug("[HTTP] GET %s -> %d, %d bytes", url, resp.status, len(text))
            return text
    except Exception as exc:
        logger.warning("[HTTP] GET %s failed: %s", url, exc)
        return ""

def _extract_proxies(text: str, pattern: str = "", protocol: str = "auto", delimiter: str = "newline") -> Set[Tuple[str, int, str, str, str]]:
    results = set()
    regex = re.compile(pattern) if pattern else IP_PORT_PATTERN
    pieces = text.split(',') if delimiter == "comma" else text.splitlines()

    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue

        parsed = parse_proxy_line(piece, protocol)
        if parsed:
            results.add((
                parsed["ip"],
                parsed["port"],
                parsed["protocol"],
                parsed.get("username", ""),
                parsed.get("password", ""),
            ))
            continue

        p_proto = protocol
        if protocol == "auto":
            piece_lower = piece.lower()
            if "http://" in piece_lower or "https://" in piece_lower:
                p_proto = "http"
            elif "socks5://" in piece_lower:
                p_proto = "socks5"
            elif "socks4://" in piece_lower:
                p_proto = "socks4"
            else:
                continue

        for match in regex.finditer(piece):
            ip = match.group(1)
            port_str = match.group(2)
            groups = match.groupdict()
            username = groups.get("username") or groups.get("user") or ""
            password = groups.get("password") or groups.get("pass") or ""
            try:
                port = int(port_str)
                if 1 <= port <= 65535:
                    parts = ip.split(".")
                    if all(0 <= int(p) <= 255 for p in parts):
                        results.add((ip, port, p_proto, username, password))
            except (ValueError, IndexError):
                continue
    return results

async def _request_via_proxy(proxy_url: str, target_url: str, timeout: int) -> Optional[Dict]:
    try:
        if proxy_url.startswith('socks'):
            connector = ProxyConnector.from_url(proxy_url)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(target_url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url, proxy=proxy_url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    return await resp.json()
    except Exception:
        return None

def _candidate_validate_urls(primary_url: str) -> List[str]:
    urls = []
    for url in [primary_url, *VALIDATE_FALLBACK_URLS]:
        if url and url not in urls:
            urls.append(url)
    return urls

async def _request_with_fallback(proxy_url: str, primary_url: str, timeout: int) -> Optional[Dict]:
    for target_url in _candidate_validate_urls(primary_url):
        data = await _request_via_proxy(proxy_url, target_url, timeout)
        if data is not None:
            return data
    return None

async def _validate_proxy(proxy: Dict, validate_url: str, timeout: int, semaphore: asyncio.Semaphore) -> Dict:
    async with semaphore:
        ip = proxy["ip"]
        port = proxy["port"]
        protocol = proxy.get("protocol", "http")
        proxy_url = build_proxy_url(proxy)

        result = {
            "ip": ip,
            "port": port,
            "protocol": protocol,
            "username": proxy.get("username", ""),
            "password": proxy.get("password", ""),
            "source": proxy.get("source", ""),
            "valid": False,
            "country": "",
            "latency": -1,
            "score": 0,
        }

        try:
            start = time.monotonic()
            data = await _request_with_fallback(proxy_url, validate_url, timeout)
            elapsed = (time.monotonic() - start) * 1000

            if data is not None:
                result["valid"] = True
                result["latency"] = round(elapsed, 1)
                result["score"] = calculate_risk_score(data)
                location = data.get("location", {})
                country_code = location.get("country_code", "")
                if country_code:
                    result["country"] = country_code.upper()
                logger.info("  ✓ VALID  %s:%d (%s) → %s, %.0fms", ip, port, protocol, result["country"] or "??", elapsed)
        except Exception:
            pass
        return result

async def _fetch_single_source(session: aiohttp.ClientSession, name: str, url: str, src_type: str, pattern: str, protocol: str = "auto", delimiter: str = "newline") -> List[Dict]:
    proxies = []
    try:
        logger.info("[FETCH] Fetching source: %s", name)
        text = await _fetch_url(session, url)
        if text:
            triples = _extract_proxies(text, pattern, protocol, delimiter)
            for ip, port, p_proto, username, password in triples:
                proxies.append({
                    "ip": ip,
                    "port": port,
                    "protocol": p_proto,
                    "username": username,
                    "password": password,
                    "source": name,
                })
            logger.info("[FETCH]   Result: %d raw proxies extracted from [%s]", len(proxies), name)
    except Exception as exc:
        logger.warning("[FETCH]   FAILED to fetch [%s]: %s", name, exc)
    return proxies

async def async_run_fetch(source_id: Optional[int] = None) -> Dict:
    storage = get_storage()
    try:
        concurrency_str = storage.get_setting("max_concurrency")
        concurrency = int(concurrency_str) if concurrency_str else MAX_VALIDATE_CONCURRENCY
    except Exception:
        concurrency = MAX_VALIDATE_CONCURRENCY

    try:
        validate_url = storage.get_setting("validate_url") or VALIDATE_URL
    except Exception:
        validate_url = VALIDATE_URL

    try:
        timeout_str = storage.get_setting("validate_timeout")
        timeout = int(timeout_str) if timeout_str else VALIDATE_TIMEOUT
    except Exception:
        timeout = VALIDATE_TIMEOUT

    logger.info("=" * 60)
    logger.info("[FETCH] Starting async fetch cycle")
    logger.info("=" * 60)

    all_sources = []
    if source_id is None:
        for name, url, src_type, pattern in BUILTIN_SOURCES:
            proto = _detect_protocol_from_url(url)
            all_sources.append((name, url, src_type, pattern, proto, "newline"))

    try:
        db_sources = await asyncio.to_thread(storage.get_sources, active_only=True)
        for s in db_sources:
            if source_id is not None and int(s.get("id", 0)) != int(source_id):
                continue
            all_sources.append((
                s["name"], s["url"], s["type"], s.get("pattern", ""),
                s.get("protocol", "auto"), s.get("delimiter", "newline")
            ))
    except Exception as exc:
        logger.warning("[FETCH] Failed to load DB sources: %s", exc)

    raw_proxies = []
    seen_keys = set()

    async with aiohttp.ClientSession() as session:
        tasks = []
        for name, url, src_type, pattern, protocol, delimiter in all_sources:
            tasks.append(_fetch_single_source(session, name, url, src_type, pattern, protocol, delimiter))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for source_proxies in results:
            if isinstance(source_proxies, list):
                for p in source_proxies:
                    key = _proxy_identity_key(p)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        raw_proxies.append(p)

    if not raw_proxies:
        logger.warning("[FETCH] No raw proxies fetched.")
        return {"sources_crawled": len(all_sources), "proxies_found": 0, "validated": 0, "stored": 0}

    logger.info("[VALIDATE] Starting validation of %d proxies (concurrency=%d)...", len(raw_proxies), concurrency)
    valid_proxies = []
    
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [_validate_proxy(p, validate_url, timeout, semaphore) for p in raw_proxies]
    
    done_count = 0
    total = len(tasks)
    invalid_count = 0

    for coro in asyncio.as_completed(tasks):
        result = await coro
        done_count += 1
        if result["valid"]:
            valid_proxies.append(result)
        else:
            invalid_count += 1
        
        if done_count % 50 == 0 or done_count == total:
            logger.info("[VALIDATE] Progress: %d/%d done (%d valid, %d invalid)", done_count, total, len(valid_proxies), invalid_count)

    stored_count = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for vp in valid_proxies:
        try:
            ok = await asyncio.to_thread(storage.add_proxy, {
                "ip": vp["ip"],
                "port": vp["port"],
                "protocol": vp["protocol"],
                "username": vp.get("username", ""),
                "password": vp.get("password", ""),
                "country": vp["country"],
                "latency": vp["latency"],
                "source": vp["source"],
                "last_check": now,
                "added_time": now,
                "score": vp["score"],
            })
            if ok:
                stored_count += 1
        except Exception as exc:
            logger.warning("[STORE] Failed to store: %s", exc)

    logger.info("[FETCH] CYCLE COMPLETE. Validated: %d, Stored: %d", len(valid_proxies), stored_count)
    return {
        "sources_crawled": len(all_sources),
        "proxies_found": len(raw_proxies),
        "validated": len(valid_proxies),
        "stored": stored_count,
    }

def run_fetch(source_id: Optional[int] = None) -> Dict:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Running in a thread with a running event loop (rare, but possible)
        future = asyncio.run_coroutine_threadsafe(async_run_fetch(source_id), loop)
        return future.result()
    else:
        # Normal case for background threads
        return asyncio.run(async_run_fetch(source_id))
