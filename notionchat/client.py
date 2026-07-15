from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from notionchat.account import NotionAccount
from notionchat.browser_fp import build_notion_request_headers
from notionchat.exceptions import NotionChatError
from notionchat.models import get_cached_alias_map, normalize_request_model, resolve_model
from notionchat.ndjson import NDJSONStreamParser, clean_notion_output_text
from notionchat.notion_http import NotionHttpClient, NotionHttpStatusError
from notionchat.thread_state import ThreadState, load_thread_state, save_thread_state
from notionchat.tools import merge_tool_calls
from notionchat.transcript import (
    _now_iso,
    build_confirm_request,
    build_full_transcript,
    build_inference_request,
    build_partial_transcript,
    new_uuid,
)

log = logging.getLogger(__name__)

MAX_AUTO_CONFIRM_ROUNDS = 3
# Notion intermittently returns trust-rule-denied under bursty concurrent load;
# a short retry usually succeeds (same pattern clients already show in access logs).
MAX_TRUST_RULE_RETRIES = 2
TRUST_RULE_RETRY_DELAY_SEC = 0.75


async def _safe_close_response(resp) -> None:
    """Close a streaming response and its underlying session."""
    with suppress(Exception):
        await resp.aclose()


def _is_retryable_trust_error(exc: BaseException) -> bool:
    return isinstance(exc, NotionChatError) and exc.status_code == 403


async def _sleep_trust_retry(attempt: int) -> None:
    delay = TRUST_RULE_RETRY_DELAY_SEC * (attempt + 1)
    log.warning(
        "Notion trust-rule / 403 (attempt %d/%d) — retrying in %.2fs",
        attempt + 1,
        MAX_TRUST_RULE_RETRIES + 1,
        delay,
    )
    await asyncio.sleep(delay)


def _empty_response_message(result, thread_id: str) -> str:
    if result.tool_calls:
        return ""
    if not result.line_count:
        return (
            "Notion returned no stream data. Check that space_id is set "
            "(run `python -m notionchat init --cookie ...`) and your cookie is fresh."
        )
    events = ", ".join(f"{k}={v}" for k, v in sorted(result.event_type_counts.items()))
    return (
        f"Notion returned empty assistant text (thread={thread_id}, events: {events or 'none'}). "
        "Your AI credits may be exhausted, or the response format changed."
    )


@dataclass(slots=True)
class ChatResult:
    text: str | None
    thread_id: str
    model: str
    tool_calls: list[dict[str, Any]] | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def build_headers(acc: NotionAccount, *, accept: str = "application/x-ndjson") -> dict[str, str]:
    return build_notion_request_headers(acc, accept=accept)


class NotionAIClient:
    def __init__(
        self,
        account: NotionAccount,
        *,
        base_url: str,
        thread_state_dir: Path,
        http_client: NotionHttpClient | None = None,
    ):
        self.account = account
        self.base_url = base_url.rstrip("/")
        self.thread_state_dir = thread_state_dir
        self._client = http_client
        self._owns_client = http_client is None

    def _get_client(self) -> NotionHttpClient:
        if self._client is None:
            self._client = NotionHttpClient()
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _prepare(
        self,
        *,
        prompt: str,
        system: str | None,
        model: str | None,
        thread_id: str | None,
        ide_agent_mode: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str], str, str, Callable[[], None]]:
        acc = self.account
        joined = f"{system}\n\n{prompt}" if system else prompt
        if not joined.strip():
            raise NotionChatError("Empty prompt", status_code=400)

        notion_model = resolve_model(
            normalize_request_model(model) or acc.default_model,
            default=acc.default_model,
            alias_map=get_cached_alias_map(),
        )
        log.info("Notion model: request=%r -> %r", model, notion_model)

        reuse_thread_id = thread_id
        prior: ThreadState | None = None
        if thread_id:
            prior = load_thread_state(thread_id, self.thread_state_dir)
            if prior.notion_model != notion_model:
                log.info(
                    "Model changed on thread %s (%r -> %r) — starting new Notion thread",
                    thread_id,
                    prior.notion_model,
                    notion_model,
                )
                reuse_thread_id = None

        if reuse_thread_id and prior:
            updated_ids = [*prior.updated_config_ids, new_uuid()]
            transcript = build_partial_transcript(
                acc,
                new_user_text=joined,
                notion_model=notion_model,
                config_id=prior.config_id,
                context_id=prior.context_id,
                updated_config_ids=updated_ids,
                original_datetime=prior.original_datetime,
                ide_agent_mode=ide_agent_mode,
            )
            active_thread_id = reuse_thread_id
            create_thread = False
            is_partial = True

            def save_state() -> None:
                prior.updated_config_ids = updated_ids
                prior.notion_model = notion_model
                prior.last_activity_iso = _now_iso(acc.timezone)
                save_thread_state(prior, self.thread_state_dir)
        else:
            config_id = new_uuid()
            context_id = new_uuid()
            first_dt = _now_iso(acc.timezone)
            transcript = build_full_transcript(
                acc,
                user_text=joined,
                notion_model=notion_model,
                config_id=config_id,
                context_id=context_id,
                now=first_dt,
                ide_agent_mode=ide_agent_mode,
            )
            active_thread_id = new_uuid()
            create_thread = True
            is_partial = False

            def save_state() -> None:
                save_thread_state(
                    ThreadState(
                        thread_id=active_thread_id,
                        config_id=config_id,
                        context_id=context_id,
                        original_datetime=first_dt,
                        notion_model=notion_model,
                    ),
                    self.thread_state_dir,
                )

        body = build_inference_request(
            acc,
            transcript=transcript,
            thread_id=active_thread_id,
            create_thread=create_thread,
            is_partial_transcript=is_partial,
        )
        headers = build_headers(acc)
        return body, headers, active_thread_id, notion_model, save_state

    def _raise_http(self, status_code: int, body: str) -> None:
        snippet = body[:500]
        if status_code in (401, 403):
            raise NotionChatError(
                f"Notion auth failed ({status_code}). Refresh token_v2 cookie. {snippet!r}",
                status_code=401,
            )
        raise NotionChatError(f"Notion API {status_code}: {snippet!r}", status_code=502)

    async def _consume_stream(
        self,
        resp,
        parser: NDJSONStreamParser,
        *,
        on_delta: Callable[[str], None] | None = None,
    ) -> None:
        # Append-only raw streaming. Re-cleaning the full buffer every chunk
        # desyncs deltas and injects fragments mid-word (e.g. "Pern" spam).
        last_raw = ""
        has_released_buffer = False
        try:
            async for line in resp.aiter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                parser.feed_line(line)
                if not on_delta:
                    continue

                raw = parser.text
                if not has_released_buffer:
                    should_release = (
                        len(raw) >= 500
                        or "\n\n" in raw
                        or "\n#" in raw
                        or raw.startswith("#")
                    )
                    if not should_release:
                        continue
                    has_released_buffer = True
                    cleaned = clean_notion_output_text(raw, finalize=True)
                    if cleaned:
                        on_delta(cleaned)
                    last_raw = raw
                    continue

                if raw.startswith(last_raw):
                    delta = raw[len(last_raw) :]
                    if delta:
                        on_delta(delta)
                        last_raw = raw
                # else: Notion rewrote earlier blocks — wait until text extends again

            if on_delta and not has_released_buffer and parser.text:
                cleaned = clean_notion_output_text(parser.text, finalize=True)
                if cleaned:
                    on_delta(cleaned)
        finally:
            await _safe_close_response(resp)

    async def _auto_confirm_pending(
        self,
        *,
        parser: NDJSONStreamParser,
        headers: dict[str, str],
        active_thread_id: str,
        on_delta: Callable[[str], None] | None = None,
    ) -> None:
        """Auto-approve Notion's "confirmation required" tool prompts (e.g. web-search
        URL-safety checks) so the agent doesn't stall waiting for a manual click.
        """
        client = self._get_client()
        url = f"{self.base_url}/runInferenceTranscript"
        last_raw = parser.text
        for _ in range(MAX_AUTO_CONFIRM_ROUNDS):
            pending = parser.pop_pending_confirmations()
            if not pending:
                return
            log.info(
                "Auto-confirming %d pending tool confirmation(s) on thread %s",
                len(pending),
                active_thread_id,
            )
            body = build_confirm_request(
                self.account,
                thread_id=active_thread_id,
                tool_result_entries=pending,
            )
            resp = None
            try:
                resp = await client.post_stream(url, json=body, headers=headers)
                if resp.status_code != 200:
                    log.warning(
                        "Auto-confirm request failed (%s): %s",
                        resp.status_code,
                        (await resp.atext())[:300],
                    )
                    return
                async for line in resp.aiter_lines():
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    parser.feed_line(line)
                    if on_delta:
                        raw = parser.text
                        if raw.startswith(last_raw):
                            delta = raw[len(last_raw) :]
                            if delta:
                                on_delta(delta)
                                last_raw = raw
            except Exception:
                log.exception("Auto-confirm request errored; leaving response as-is")
                return
            finally:
                if resp is not None:
                    await _safe_close_response(resp)

    async def _run_inference(
        self,
        *,
        prompt: str,
        system: str | None,
        model: str | None,
        thread_id: str | None,
        ide_agent_mode: bool,
        on_delta: Callable[[str], None] | None = None,
        tools_active: bool,
        client_tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        body, headers, active_thread_id, notion_model, save_state = self._prepare(
            prompt=prompt,
            system=system,
            model=model,
            thread_id=thread_id,
            ide_agent_mode=ide_agent_mode,
        )
        url = f"{self.base_url}/runInferenceTranscript"
        parser = NDJSONStreamParser()
        client = self._get_client()
        resp = None
        try:
            resp = await client.post_stream(url, json=body, headers=headers)
            if resp.status_code != 200:
                body_text = await resp.atext()
                self._raise_http(resp.status_code, body_text)
            await self._consume_stream(resp, parser, on_delta=on_delta)
            resp = None  # closed inside _consume_stream
            await self._auto_confirm_pending(
                parser=parser,
                headers=headers,
                active_thread_id=active_thread_id,
                on_delta=on_delta,
            )
        except NotionHttpStatusError as e:
            self._raise_http(e.status_code, e.body)
        except NotionChatError:
            raise
        except Exception as e:
            raise NotionChatError(f"Notion transport error: {e}", status_code=502) from e
        finally:
            if resp is not None:
                await _safe_close_response(resp)

        result = parser.finalize()
        raw_text = result.text
        content, tool_calls = merge_tool_calls(
            text=raw_text,
            ndjson_tool_calls=result.tool_calls,
            tools_active=tools_active,
            client_tools=client_tools,
            prompt=prompt,
            ide_agent=ide_agent_mode,
        )
        if not content and not tool_calls:
            raise NotionChatError(_empty_response_message(result, active_thread_id), status_code=502)
        save_state()
        return ChatResult(
            text=raw_text if ide_agent_mode else content,
            thread_id=active_thread_id,
            model=result.notion_model or notion_model,
            tool_calls=tool_calls or None,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        thread_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        tools_active: bool = False,
        ide_agent_mode: bool = False,
        client_tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        last_err: NotionChatError | None = None
        for attempt in range(MAX_TRUST_RULE_RETRIES + 1):
            try:
                return await self._run_inference(
                    prompt=prompt,
                    system=system,
                    model=model,
                    thread_id=thread_id,
                    ide_agent_mode=ide_agent_mode,
                    on_delta=on_delta,
                    tools_active=tools_active,
                    client_tools=client_tools,
                )
            except NotionChatError as e:
                if _is_retryable_trust_error(e) and attempt < MAX_TRUST_RULE_RETRIES:
                    last_err = e
                    await _sleep_trust_retry(attempt)
                    continue
                raise
        assert last_err is not None
        raise last_err

    async def stream_deltas(
        self,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        thread_id: str | None = None,
        tools_active: bool = False,
        ide_agent_mode: bool = False,
        client_tools: list[dict[str, Any]] | None = None,
        buffer_until_complete: bool = False,
    ) -> tuple[AsyncIterator[str], str, Callable[[], ChatResult]]:
        body, headers, active_thread_id, notion_model, save_state = self._prepare(
            prompt=prompt,
            system=system,
            model=model,
            thread_id=thread_id,
            ide_agent_mode=ide_agent_mode,
        )
        url = f"{self.base_url}/runInferenceTranscript"
        client = self._get_client()
        # Mutable holders so retries can refresh prepare() state.
        state: dict[str, Any] = {
            "body": body,
            "headers": headers,
            "active_thread_id": active_thread_id,
            "notion_model": notion_model,
            "save_state": save_state,
        }
        parser = NDJSONStreamParser()
        last_emitted = ""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        http_error: list[BaseException] = []
        replace_mark = "<<<MUGHU_STREAM_REPLACE>>>\n"

        async def producer() -> None:
            nonlocal last_emitted, parser
            emitted_any = False
            try:
                for attempt in range(MAX_TRUST_RULE_RETRIES + 1):
                    has_released_buffer = False
                    last_emitted = ""
                    parser = NDJSONStreamParser()
                    resp = None
                    try:
                        resp = await client.post_stream(
                            url, json=state["body"], headers=state["headers"]
                        )
                        if resp.status_code != 200:
                            self._raise_http(resp.status_code, await resp.atext())
                        async for line in resp.aiter_lines():
                            if isinstance(line, bytes):
                                line = line.decode("utf-8", errors="replace")
                            parser.feed_line(line)

                            if buffer_until_complete:
                                # Collect only — final cleaned text is emitted after
                                # finalize in the OpenAI SSE layer.
                                continue

                            raw = parser.text
                            cleaned = clean_notion_output_text(raw, finalize=False)
                            if not cleaned:
                                continue

                            if not has_released_buffer:
                                should_release = (
                                    len(cleaned) >= 80
                                    or "\n\n" in cleaned
                                    or "\n#" in cleaned
                                    or cleaned.startswith("#")
                                )
                                if not should_release:
                                    continue
                                has_released_buffer = True
                                emitted_any = True
                                await queue.put(cleaned)
                                last_emitted = cleaned
                                continue

                            if cleaned == last_emitted:
                                continue

                            if cleaned.startswith(last_emitted):
                                delta = cleaned[len(last_emitted) :]
                                if delta:
                                    emitted_any = True
                                    await queue.put(delta)
                                    last_emitted = cleaned
                            else:
                                # Notion patched an earlier block — full snapshot
                                # so append clients can resync (live streaming).
                                emitted_any = True
                                await queue.put(replace_mark + cleaned)
                                last_emitted = cleaned

                        if not buffer_until_complete:
                            if not has_released_buffer and parser.text:
                                cleaned = clean_notion_output_text(
                                    parser.text, finalize=True
                                )
                                if cleaned:
                                    emitted_any = True
                                    await queue.put(cleaned)
                                    last_emitted = cleaned
                            elif has_released_buffer and parser.text:
                                final_cleaned = clean_notion_output_text(
                                    parser.text, finalize=True
                                )
                                if final_cleaned and final_cleaned != last_emitted:
                                    emitted_any = True
                                    if final_cleaned.startswith(last_emitted):
                                        await queue.put(
                                            final_cleaned[len(last_emitted) :]
                                        )
                                    else:
                                        await queue.put(replace_mark + final_cleaned)
                                    last_emitted = final_cleaned

                        if parser.pending_tool_confirmations:
                            def _emit(delta: str) -> None:
                                if buffer_until_complete:
                                    return
                                asyncio.get_event_loop().create_task(queue.put(delta))

                            await self._auto_confirm_pending(
                                parser=parser,
                                headers=state["headers"],
                                active_thread_id=state["active_thread_id"],
                                on_delta=None if buffer_until_complete else _emit,
                            )
                        return
                    except BaseException as e:
                        if (
                            _is_retryable_trust_error(e)
                            and attempt < MAX_TRUST_RULE_RETRIES
                            and not emitted_any
                        ):
                            await _sleep_trust_retry(attempt)
                            (
                                state["body"],
                                state["headers"],
                                state["active_thread_id"],
                                state["notion_model"],
                                state["save_state"],
                            ) = self._prepare(
                                prompt=prompt,
                                system=system,
                                model=model,
                                thread_id=thread_id,
                                ide_agent_mode=ide_agent_mode,
                            )
                            continue
                        http_error.append(e)
                        return
                    finally:
                        if resp is not None:
                            await _safe_close_response(resp)
            finally:
                await queue.put(None)

        async def consumer() -> AsyncIterator[str]:
            task = asyncio.create_task(producer())
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    yield chunk
                if http_error:
                    raise http_error[0]
            finally:
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                else:
                    await task

        def finalize_result() -> ChatResult:
            result = parser.finalize()
            raw_text = result.text
            content, tool_calls = merge_tool_calls(
                text=raw_text,
                ndjson_tool_calls=result.tool_calls,
                tools_active=tools_active,
                client_tools=client_tools,
                prompt=prompt,
                ide_agent=ide_agent_mode,
            )
            if not content and not tool_calls:
                raise NotionChatError(
                    _empty_response_message(result, state["active_thread_id"]),
                    status_code=502,
                )
            state["save_state"]()
            return ChatResult(
                text=raw_text if ide_agent_mode else content,
                thread_id=state["active_thread_id"],
                model=result.notion_model or state["notion_model"],
                tool_calls=tool_calls or None,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        return consumer(), active_thread_id, finalize_result

    async def fetch_available_models(self) -> dict[str, Any]:
        url = f"{self.base_url}/getAvailableModels"
        headers = build_headers(self.account, accept="application/json")
        client = self._get_client()
        try:
            return await client.post_json(
                url,
                json={"spaceId": self.account.space_id},
                headers=headers,
            )
        except NotionHttpStatusError as e:
            raise NotionChatError(f"getAvailableModels failed: {e.status_code}", status_code=502) from e
