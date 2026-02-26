"""Web tools: web_search and web_fetch."""

import asyncio
import html
import json
import os
import re
import socket
import ssl
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import httpx

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


def _runtime_env_diagnostics() -> dict[str, Any]:
    """Return non-sensitive runtime env context useful for network debugging."""
    verify = _tls_verify_target()
    verify_source = _tls_verify_source()
    certifi_path = _certifi_ca_path()
    return {
        "has_http_proxy": bool(os.getenv("HTTP_PROXY") or os.getenv("http_proxy")),
        "has_https_proxy": bool(os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")),
        "has_no_proxy": bool(os.getenv("NO_PROXY") or os.getenv("no_proxy")),
        "ssl_cert_file_set": bool(os.getenv("SSL_CERT_FILE")),
        "ssl_cert_dir_set": bool(os.getenv("SSL_CERT_DIR")),
        "requests_ca_bundle_set": bool(os.getenv("REQUESTS_CA_BUNDLE")),
        "curl_ca_bundle_set": bool(os.getenv("CURL_CA_BUNDLE")),
        "tls_verify_source": verify_source,
        "tls_verify_path": str(verify) if isinstance(verify, str) else certifi_path,
        "tls_verify_is_ssl_context": isinstance(verify, ssl.SSLContext),
    }


def _certifi_ca_path() -> str | None:
    """Return certifi CA bundle path if available."""
    try:
        import certifi  # type: ignore

        return certifi.where()
    except Exception:
        return None


@lru_cache(maxsize=1)
def _tls_verify_target() -> bool | str | ssl.SSLContext:
    """Resolve TLS CA bundle path with sensible defaults.

    Priority:
    1. Explicit env override (SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE)
    2. Combined context: certifi roots + system roots (best out-of-box behavior)
    3. System defaults (httpx/ssl fallback)
    """
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        val = os.getenv(key)
        if val:
            return val

    certifi_path = _certifi_ca_path()
    if certifi_path:
        try:
            # Start from certifi for consistency in minimal images, then add
            # system trust anchors (including corporate/intercept roots).
            ctx = ssl.create_default_context(cafile=certifi_path)
            try:
                ctx.load_default_certs()
            except Exception:
                pass
            return ctx
        except Exception:
            return certifi_path

    return True


def _tls_verify_source() -> str:
    """Human-readable source for TLS CA verification settings."""
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        if os.getenv(key):
            return f"env:{key}"
    if _certifi_ca_path():
        return "certifi+system"
    return "system-default"


def _failure_hint(
    *,
    error_type: str,
    error_text: str,
    dns_ok: bool | None,
    scheme: str,
    status_code: int | None = None,
) -> str:
    """Generate an actionable hint for common web_fetch failures."""
    msg = (error_text or "").lower()
    et = (error_type or "").lower()

    if status_code is not None:
        if status_code in (401, 403):
            return "Target denied access (auth/permissions). Try another source or authenticated endpoint."
        if status_code == 404:
            return "URL returned 404 (not found). Verify path or redirects."
        if status_code in (429,):
            return "Target is rate-limiting requests. Retry later or reduce frequency."
        if 500 <= status_code <= 599:
            return "Target server failed (5xx). Retry later; issue is likely upstream."

    if "certificate verify failed" in msg or "ssl: cert" in msg or "tlsv1" in msg:
        return (
            "TLS certificate verification failed. Check CA trust on the Kyber runtime "
            "(ca-certificates, corporate MITM proxy root cert, system clock)."
        )

    dns_markers = (
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "getaddrinfo failed",
    )
    if not dns_ok or any(marker in msg for marker in dns_markers):
        return (
            "DNS resolution failed in the Kyber runtime. Compare DNS/proxy env for the "
            "service/process running Kyber vs your interactive shell."
        )

    if "timed out" in msg or "timeout" in msg or et in ("readtimeout", "connecttimeout", "timeoutexception"):
        return "Request timed out. Target may be slow/blocked. Retry or increase timeout."

    if "connection refused" in msg:
        return "Connection was refused by the target host/port."

    if "network is unreachable" in msg or "no route to host" in msg:
        return "Network path to target is unavailable from the Kyber runtime."

    if scheme == "https" and et == "connecterror":
        return (
            "HTTPS connection failed before response. Check proxy/TLS settings and "
            "whether this runtime has outbound internet access."
        )

    return "Request failed. Check diagnostics.errorType, diagnostics.dns, and proxy/SSL env flags."


async def _dns_diagnostics(hostname: str | None, port: int | None) -> dict[str, Any]:
    """Resolve hostname using the same runtime resolver used by this process."""
    if not hostname:
        return {"checked": False, "ok": False, "error": "No hostname in URL"}

    lookup_port = port or 443
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            hostname,
            lookup_port,
            type=socket.SOCK_STREAM,
        )
        addrs = sorted({entry[4][0] for entry in infos if entry and len(entry) >= 5 and entry[4]})
        return {
            "checked": True,
            "ok": True,
            "hostname": hostname,
            "port": lookup_port,
            "resolved_count": len(addrs),
            "addresses": addrs[:6],
        }
    except Exception as e:
        return {
            "checked": True,
            "ok": False,
            "hostname": hostname,
            "port": lookup_port,
            "errorType": type(e).__name__,
            "error": str(e),
        }


async def _build_fetch_error_payload(url: str, exc: Exception) -> dict[str, Any]:
    """Build structured diagnostics for web_fetch failures."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    scheme = parsed.scheme or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    error_type = type(exc).__name__
    error_text = str(exc)
    status_code: int | None = None

    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status_code = exc.response.status_code

    dns = await _dns_diagnostics(hostname, port)
    hint = _failure_hint(
        error_type=error_type,
        error_text=error_text,
        dns_ok=dns.get("ok"),
        scheme=scheme,
        status_code=status_code,
    )

    payload: dict[str, Any] = {
        "error": f"{error_type}: {error_text}",
        "errorType": error_type,
        "url": url,
        "diagnostics": {
            "hostname": hostname,
            "scheme": scheme,
            "port": port,
            "http_status": status_code,
            "dns": dns,
            "env": _runtime_env_diagnostics(),
            "hint": hint,
        },
    }
    return payload


class WebSearchTool(Tool):
    """Search the web using Brave Search API."""
    
    toolset = "web"
    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }
    
    def __init__(self, api_key: str | None = None, max_results: int = 5):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self.max_results = max_results
    
    def is_available(self) -> bool:
        """Only available when BRAVE_API_KEY is set."""
        return bool(self.api_key or os.environ.get("BRAVE_API_KEY", ""))
    
    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        if not self.api_key:
            return "Error: BRAVE_API_KEY not configured"
        
        try:
            n = min(max(count or self.max_results, 1), 10)
            async with httpx.AsyncClient(verify=_tls_verify_target()) as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    timeout=10.0
                )
                r.raise_for_status()
            
            results = r.json().get("web", {}).get("results", [])
            if not results:
                return f"No results for: {query}"
            
            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""
    
    toolset = "web"
    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }
    
    def __init__(self, max_chars: int = 50000):
        self.max_chars = max_chars
    
    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:
        from readability import Document

        max_chars = maxChars or self.max_chars

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url})

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0,
                verify=_tls_verify_target(),
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
            
            ctype = r.headers.get("content-type", "")
            
            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"
            
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            
            return json.dumps({"url": url, "finalUrl": str(r.url), "status": r.status_code,
                              "extractor": extractor, "truncated": truncated, "length": len(text), "text": text})
        except Exception as e:
            payload = await _build_fetch_error_payload(url, e)
            return json.dumps(payload, ensure_ascii=False)
    
    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(r'<a\s+[^>]*href=["\']((?!#)[^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))


# ── Self-register on import ─────────────────────────────────────────
registry.register(WebSearchTool())
registry.register(WebFetchTool())
