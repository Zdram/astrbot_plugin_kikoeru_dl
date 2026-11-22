import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def origin(url: str) -> tuple[str, str, int]:
    if "\\" in url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("KikoFlu 地址包含无效字符。")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError("KikoFlu 地址必须是没有用户名或密码的 HTTP(S) URL。")
    port = parts.port
    if port == 0:
        raise ValueError("KikoFlu 地址的端口必须大于 0。")
    return scheme, parts.hostname.rstrip(".").lower(), port or (443 if scheme == "https" else 80)


def open_response(
    backend: str,
    url: str,
    headers: dict[str, str],
    media_origins: set[tuple[str, str, int]],
    proxy: str,
    method: str = "GET",
    body: dict | None = None,
):
    """Open a backend or approved media URL with per-hop credential scoping.

    Args:
        backend: Configured API URL.
        url: Initial request URL.
        headers: Headers to use for the configured API.
        media_origins: Allowed HTTPS origins for credential-free media requests.
        proxy: Optional proxy. Empty means direct.
        method: HTTP method.
        body: Optional JSON request body.

    Returns:
        Open HTTP response. The caller closes it.
    """
    backend_origin = origin(backend)
    secret = headers.get("Authorization", "").removeprefix("Bearer ")
    private_headers = {"authorization", "cookie", "proxy-authorization"}

    def scoped_request(target: str, request_headers: dict[str, str], verb: str, data: bytes | None):
        target_origin = origin(target)
        if target_origin == backend_origin:
            return Request(target, headers=request_headers, method=verb, data=data)
        if verb != "GET" or target_origin[0] != "https" or target_origin not in media_origins:
            raise ValueError("已阻止发往未配置地址的 KikoFlu 请求。")
        parts = urlsplit(target)
        fields = parse_qsl(parts.query, keep_blank_values=True)
        filtered = [
            (key, value)
            for key, value in fields
            if key.casefold() not in {"token", "auth_token", "authorization", "cookie"}
            and (not secret or (secret not in key and secret not in value))
        ]
        if secret and secret in unquote(parts.path):
            raise ValueError("已阻止包含 KikoFlu 凭据的媒体地址。")
        safe_url = parts._replace(
            query=urlencode(filtered) if filtered != fields else parts.query,
            fragment="",
        ).geturl()
        outgoing = {key: value for key, value in request_headers.items() if key.casefold() not in private_headers}
        return Request(safe_url, headers=outgoing, method=verb)

    class ScopedRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            redirected = super().redirect_request(req, fp, code, msg, response_headers, newurl)
            if redirected is None:
                return None
            if origin(newurl) != backend_origin and req.get_method() != "GET":
                raise ValueError("已阻止 KikoFlu 写操作重定向到其他地址。")
            return scoped_request(newurl, dict(redirected.header_items()), redirected.get_method(), redirected.data)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    request = scoped_request(url, headers, method, data)
    for route in (proxy, "") if proxy else ("",):
        proxies = {"http": route, "https": route} if route else {}
        try:
            return build_opener(ProxyHandler(proxies), ScopedRedirect()).open(request, timeout=60)
        except HTTPError:
            raise
        except (URLError, TimeoutError):
            if not route or method != "GET":
                raise
    raise RuntimeError("KikoFlu HTTP request did not return a response")
