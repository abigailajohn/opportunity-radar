from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from opportunity_radar.html_preparation import prepare_html


DEFAULT_USER_AGENT = "OpportunityRadar/0.1 (+manual opportunity evaluation)"
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class FetchFailureKind(StrEnum):
    INVALID_URL = "invalid_url"
    TIMEOUT = "timeout"
    CONNECTION = "connection_error"
    HTTP_STATUS = "http_status"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_BODY = "invalid_response_body"


class FetchError(RuntimeError):
    kind: FetchFailureKind

    def __init__(self, kind: FetchFailureKind, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class FetchTimeoutError(FetchError):
    def __init__(self, message: str = "request timed out") -> None:
        super().__init__(FetchFailureKind.TIMEOUT, message)


class FetchConnectionError(FetchError):
    def __init__(self, message: str = "could not connect to host") -> None:
        super().__init__(FetchFailureKind.CONNECTION, message)


class FetchHttpStatusError(FetchError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(FetchFailureKind.HTTP_STATUS, f"HTTP response status {status_code}")


class UnsupportedContentTypeError(FetchError):
    def __init__(self, content_type: str) -> None:
        self.content_type = content_type
        super().__init__(
            FetchFailureKind.UNSUPPORTED_CONTENT_TYPE,
            f"unsupported Content-Type: {content_type or '<missing>'}",
        )


class ResponseTooLargeError(FetchError):
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        super().__init__(
            FetchFailureKind.RESPONSE_TOO_LARGE,
            f"response exceeds maximum size of {maximum_bytes} bytes",
        )


class InvalidResponseBodyError(FetchError):
    def __init__(self, message: str = "response body is empty or invalid") -> None:
        super().__init__(FetchFailureKind.INVALID_BODY, message)


@dataclass(frozen=True)
class RedirectHop:
    url: str
    status_code: int


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    raw_html: str
    status_code: int = 200
    content_type: str = "text/html"
    page_title: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    byte_count: int = 0
    character_count: int = 0
    redirect_history: tuple[RedirectHop, ...] = ()
    response_headers: dict[str, str] = field(default_factory=dict)
    cleaned_text: str = ""

    @property
    def content(self) -> str:
        """Compatibility alias for fake extractors built before live fetching."""
        return self.raw_html


def _media_type(value: str) -> str:
    return value.split(";", maxsplit=1)[0].strip().casefold()


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise FetchError(FetchFailureKind.INVALID_URL, "URL must use HTTP or HTTPS and include a host")


def _page_title(html: str) -> str | None:
    title = BeautifulSoup(html, "html.parser").title
    if title is None:
        return None
    normalized = " ".join(title.get_text(" ", strip=True).split())
    return normalized or None


class HttpPageFetcher:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
        maximum_bytes: int = 2_000_000,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        self._client = client
        self.timeout = httpx.Timeout(timeout_seconds)
        self.maximum_bytes = maximum_bytes
        self.headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}

    def fetch(self, url: str) -> FetchedPage:
        _validate_url(url)
        if self._client is not None:
            return self._fetch_with_client(self._client, url)
        with httpx.Client() as client:
            return self._fetch_with_client(client, url)

    def _fetch_with_client(self, client: httpx.Client, url: str) -> FetchedPage:
        try:
            with client.stream(
                "GET",
                url,
                headers=self.headers,
                timeout=self.timeout,
                follow_redirects=True,
            ) as response:
                if not response.is_success:
                    raise FetchHttpStatusError(response.status_code)
                content_type = _media_type(response.headers.get("content-type", ""))
                if content_type not in HTML_CONTENT_TYPES:
                    raise UnsupportedContentTypeError(content_type)
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > self.maximum_bytes:
                    raise ResponseTooLargeError(self.maximum_bytes)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.maximum_bytes:
                        raise ResponseTooLargeError(self.maximum_bytes)
                if not body:
                    raise InvalidResponseBodyError()
                encoding = response.encoding or "utf-8"
                try:
                    html = bytes(body).decode(encoding)
                except (LookupError, UnicodeDecodeError) as exc:
                    raise InvalidResponseBodyError(f"could not decode response body as {encoding}") from exc
                final_url = str(response.url)
                return FetchedPage(
                    requested_url=url,
                    final_url=final_url,
                    status_code=response.status_code,
                    content_type=content_type,
                    raw_html=html,
                    page_title=_page_title(html),
                    fetched_at=datetime.now(timezone.utc),
                    byte_count=len(body),
                    character_count=len(html),
                    redirect_history=tuple(
                        RedirectHop(str(item.url), item.status_code) for item in response.history
                    ),
                    response_headers={key: value for key, value in response.headers.items()},
                    cleaned_text=prepare_html(html, base_url=final_url),
                )
        except FetchError:
            raise
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError() from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise FetchConnectionError(str(exc) or "could not connect to host") from exc
        except httpx.RequestError as exc:
            raise FetchConnectionError(str(exc) or "HTTP request failed") from exc
