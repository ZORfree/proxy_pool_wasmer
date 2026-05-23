"""
Helpers for parsing and formatting proxy endpoints.
"""
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlsplit

SUPPORTED_PROXY_PROTOCOLS = {"http", "https", "socks4", "socks5"}


def clean_protocol(protocol: Optional[str], default: str = "http") -> str:
    value = (protocol or default or "http").strip().lower()
    return default if value == "auto" else value


def proxy_has_auth(proxy: Dict[str, Any]) -> bool:
    return bool(str(proxy.get("username") or "") and str(proxy.get("password") or ""))


def proxy_identity_key(proxy: Dict[str, Any]) -> tuple:
    return (
        str(proxy.get("ip") or proxy.get("host") or ""),
        int(proxy["port"]),
        clean_protocol(proxy.get("protocol")),
        str(proxy.get("username") or ""),
        str(proxy.get("password") or ""),
    )


def build_proxy_url(proxy: Dict[str, Any]) -> str:
    protocol = clean_protocol(proxy.get("protocol"))
    host = str(proxy.get("ip") or proxy.get("host") or "")
    port = int(proxy["port"])
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")

    auth = ""
    if username or password:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    return f"{protocol}://{auth}{host}:{port}"


def parse_proxy_line(line: str, default_protocol: str = "auto") -> Optional[Dict[str, Any]]:
    raw = (line or "").strip()
    if not raw:
        return None

    protocol = clean_protocol(default_protocol, default="auto")
    has_scheme = "://" in raw
    if not has_scheme and protocol == "auto":
        return None

    url = raw if has_scheme else f"{protocol}://{raw}"
    try:
        parsed = urlsplit(url)
        scheme = clean_protocol(parsed.scheme or protocol)
        port = parsed.port
    except ValueError:
        return None

    if scheme not in SUPPORTED_PROXY_PROTOCOLS or not parsed.hostname or port is None:
        return None

    return {
        "ip": parsed.hostname,
        "port": port,
        "protocol": scheme,
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }
