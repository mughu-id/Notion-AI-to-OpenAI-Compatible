from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from curl_cffi.requests import AsyncSession, Response

log = logging.getLogger(__name__)

BROWSER_IMPERSONATE = "chrome"
DEFAULT_TIMEOUT = 300.0


# --- curl_cffi 0.15.0 + Python 3.14 workaround ---------------------------------
# The AsyncCurl._pop_future method calls lib.curl_multi_remove_handle with
# curl._curl, which can be None during cleanup callbacks, raising
# "TypeError: initializer for ctype 'void *' must be a cdata pointer, not NoneType".
# Monkey-patch it to skip None handles. Applied once at import.
try:
    from curl_cffi.aio import AsyncCurl as _AsyncCurl
    from curl_cffi._wrapper import lib, ffi  # type: ignore

    _original_pop_future = _AsyncCurl._pop_future

    def _safe_pop_future(self, curl):  # noqa: ANN001, ANN202
        curl_ptr = getattr(curl, "_curl", None)
        if curl_ptr is None or self._curlm is None:
            return self._curl2future.pop(curl, None)
        errcode = lib.curl_multi_remove_handle(self._curlm, curl_ptr)
        try:
            self._check_error(errcode)
        except Exception:
            pass
        self._curl2curl.pop(curl_ptr, None)
        return self._curl2future.pop(curl, None)

    if getattr(_original_pop_future, "__name__", "") != "_safe_pop_future":
        _AsyncCurl._pop_future = _safe_pop_future
        log.debug("Patched AsyncCurl._pop_future for curl_cffi 0.15.0 NoneType bug")
except Exception as _patch_err:  # pragma: no cover
    log.warning("Could not patch curl_cffi AsyncCurl: %s", _patch_err)
# -----------------------------------------------------------------------------


class NotionHttpStatusError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]!r}")


class NotionStreamResponse:
    """Wrapper around curl_cffi Response that owns its session for cleanup."""

    __slots__ = ("_resp", "_session", "_closed")

    def __init__(self, resp: Response, session: AsyncSession) -> None:
        self._resp = resp
        self._session = session
        self._closed = False

    @property
    def status_code(self) -> int:
        return self._resp.status_code

    async def aiter_lines(self):  # noqa: ANN202
        async for line in self._resp.aiter_lines():
            yield line

    async def atext(self) -> str:
        return await self._resp.atext()

    def json(self) -> Any:
        return self._resp.json()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            self._resp.close()
        # Let curl_cffi's cleanup callbacks drain before closing the session.
        await asyncio.sleep(0)
        with suppress(Exception):
            await self._session.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            self._resp.close()


class NotionHttpClient:
    """Impersonates Chrome TLS — required for Notion AI on Business plans.

    Each request gets a fresh AsyncSession to avoid curl_cffi socket-pipe
    corruption on Windows ProactorEventLoop (WinError 10054). Sessions are
    closed after the response is consumed.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def aclose(self) -> None:
        return

    async def _request(
        self,
        url: str,
        *,
        method: str,
        json: dict[str, Any],
        headers: dict[str, str],
        stream: bool,
    ) -> NotionStreamResponse:
        session = AsyncSession(timeout=self._timeout)
        try:
            resp = await session.request(
                method,
                url,
                json=json,
                headers=headers,
                impersonate=BROWSER_IMPERSONATE,
                stream=stream,
            )
            return NotionStreamResponse(resp, session)
        except BaseException:
            with suppress(Exception):
                await asyncio.sleep(0)
                await session.close()
            raise

    async def post_json(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
        wrapper = await self._request(
            url,
            method="POST",
            json=json,
            headers=headers,
            stream=False,
        )
        try:
            if wrapper.status_code != 200:
                raise NotionHttpStatusError(wrapper.status_code, await wrapper.atext())
            return wrapper.json()
        finally:
            await wrapper.aclose()

    async def post_stream(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> NotionStreamResponse:
        return await self._request(
            url,
            method="POST",
            json=json,
            headers=headers,
            stream=True,
        )
