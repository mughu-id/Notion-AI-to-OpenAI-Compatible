from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests

from notionchat.account import NotionAccount, parse_browser_cookie, save_notion_account
from notionchat.browser_fp import (
    DEFAULT_CLIENT_VERSION,
    DEFAULT_USER_AGENT,
    build_notion_request_headers,
    fingerprint_kwargs,
)
from notionchat.exceptions import NotionChatError

log = logging.getLogger(__name__)

BASE_URL = "https://app.notion.com/api/v3"


@dataclass(slots=True, frozen=True)
class Workspace:
    space_id: str
    space_view_id: str
    space_name: str
    domain: str = ""


def _extract_user(record_map: dict[str, Any], user_id: str) -> tuple[str, str]:
    users = record_map.get("notion_user") or {}
    entry = users.get(user_id) or {}
    value = (entry.get("value") or {}).get("value") or {}
    name_list = value.get("name") or []
    name = name_list[0][0] if name_list and name_list[0] else ""
    email = value.get("email", "")
    return name, email


def _fetch_load_user_content(
    cookie: str,
    *,
    user_agent: str | None = None,
    client_version: str | None = None,
    impersonate: str | None = None,
) -> tuple[dict[str, Any], str, str | None, str, str]:
    """Call Notion loadUserContent and return parsed session fields."""
    parsed = parse_browser_cookie(cookie)
    token = parsed.get("token_v2")
    if not token:
        raise NotionChatError("Cookie missing token_v2", status_code=400)

    user_id = parsed.get("notion_user_id") or None
    browser_id = parsed.get("notion_browser_id") or str(uuid.uuid4())
    device_id = parsed.get("device_id") or str(uuid.uuid4())
    fp = fingerprint_kwargs(user_agent=user_agent, client_version=client_version)

    probe = NotionAccount(
        token_v2=token,
        full_cookie=cookie.strip().rstrip(";"),
        user_id=user_id or "",
        browser_id=browser_id,
        device_id=device_id,
        client_version=fp["client_version"],
        user_agent=fp["user_agent"],
        extras={"sec_ch_ua": fp["sec_ch_ua"]},
    )
    headers = build_notion_request_headers(
        probe,
        accept="application/json",
        referer="https://app.notion.com/",
    )
    resp = requests.post(
        f"{BASE_URL}/loadUserContent",
        json={"cursor": {"stack": []}, "limit": 100},
        headers=headers,
        impersonate=impersonate or fp["impersonate"],
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise NotionChatError(
            f"loadUserContent failed ({resp.status_code}): {resp.text[:300]!r}",
            status_code=502,
        )
    return resp.json(), token, user_id, browser_id, device_id


def list_workspaces_from_cookie_sync(cookie: str) -> list[Workspace]:
    """Return Notion workspaces available to the browser cookie."""
    data, _, user_id, _, _ = _fetch_load_user_content(cookie)
    record_map = data.get("recordMap") or {}
    if not user_id:
        for uid in record_map.get("notion_user") or {}:
            user_id = uid
            break
    if not user_id:
        raise NotionChatError("Could not determine notion_user_id", status_code=502)
    workspaces = _extract_workspaces(record_map)
    if not workspaces:
        raise NotionChatError("No workspaces found for this account", status_code=502)
    return workspaces


def _extract_workspaces(record_map: dict[str, Any]) -> list[Workspace]:
    spaces: list[Workspace] = []
    space_map = record_map.get("space") or {}
    view_map = record_map.get("space_view") or {}
    for view_id, view_entry in view_map.items():
        view_val = (view_entry.get("value") or {}).get("value") or {}
        space_id = view_val.get("space_id") or view_val.get("parent_id")
        if not space_id:
            continue
        space_entry = space_map.get(space_id) or {}
        space_val = (space_entry.get("value") or {}).get("value") or {}
        name_list = space_val.get("name") or []
        name = name_list[0][0] if name_list and name_list[0] else space_id
        domain = space_val.get("domain", "") or ""
        spaces.append(
            Workspace(
                space_id=space_id,
                space_view_id=view_id,
                space_name=name,
                domain=domain,
            )
        )
    return spaces


def _pick_workspace(workspaces: list[Workspace], space_name: str | None) -> Workspace:
    """Resolve a workspace by name, otherwise auto-select the first available one."""
    if not workspaces:
        raise NotionChatError("No workspaces found for this account", status_code=502)

    names = ", ".join(w.space_name for w in workspaces)
    if space_name:
        needle = space_name.strip().lower()
        matches = [w for w in workspaces if w.space_name.lower() == needle]
        if matches:
            return matches[0]
        # Partial / substring match (e.g. env has stale name from another account)
        partial = [w for w in workspaces if needle in w.space_name.lower() or w.space_name.lower() in needle]
        if len(partial) == 1:
            log.warning(
                "Workspace %r not exact; using closest match %r",
                space_name,
                partial[0].space_name,
            )
            return partial[0]
        log.warning(
            "Workspace %r not found (available: %s) — auto-selecting %r",
            space_name,
            names,
            workspaces[0].space_name,
        )
        return workspaces[0]

    if len(workspaces) > 1:
        log.info(
            "Multiple workspaces found (%s) — auto-selecting %r "
            "(set NOTION_SPACE_NAME or run `notion setup` to choose)",
            names,
            workspaces[0].space_name,
        )
    return workspaces[0]


def bootstrap_from_cookie_sync(
    cookie: str,
    *,
    space_name: str | None = None,
    account_path: str = "notion_account.json",
    user_agent: str | None = None,
    client_version: str | None = None,
) -> NotionAccount:
    """Sync variant used during server startup when space_id is missing."""
    fp = fingerprint_kwargs(user_agent=user_agent, client_version=client_version)
    data, token, user_id, browser_id, device_id = _fetch_load_user_content(
        cookie,
        user_agent=fp["user_agent"],
        client_version=fp["client_version"],
        impersonate=fp["impersonate"],
    )

    return _account_from_load_user_content(
        cookie=cookie.strip().rstrip(";"),
        data=data,
        token=token,
        user_id=user_id,
        browser_id=browser_id,
        device_id=device_id,
        space_name=space_name,
        account_path=account_path,
        user_agent=fp["user_agent"],
        client_version=fp["client_version"],
        sec_ch_ua=fp["sec_ch_ua"],
    )


def _account_from_load_user_content(
    *,
    cookie: str,
    data: dict[str, Any],
    token: str,
    user_id: str | None,
    browser_id: str,
    device_id: str,
    space_name: str | None,
    account_path: str,
    user_agent: str = DEFAULT_USER_AGENT,
    client_version: str = DEFAULT_CLIENT_VERSION,
    sec_ch_ua: str | None = None,
) -> NotionAccount:
    record_map = data.get("recordMap") or {}
    if not user_id:
        for uid in record_map.get("notion_user") or {}:
            user_id = uid
            break
    if not user_id:
        raise NotionChatError("Could not determine notion_user_id", status_code=502)

    user_name, user_email = _extract_user(record_map, user_id)
    workspaces = _extract_workspaces(record_map)
    if not workspaces:
        raise NotionChatError("No workspaces found for this account", status_code=502)

    chosen = _pick_workspace(workspaces, space_name)

    extras: dict[str, Any] = {}
    if sec_ch_ua:
        extras["sec_ch_ua"] = sec_ch_ua

    acc = NotionAccount(
        token_v2=token,
        full_cookie=cookie,
        user_id=user_id,
        user_name=user_name,
        user_email=user_email,
        space_id=chosen.space_id,
        space_name=chosen.space_name,
        space_view_id=chosen.space_view_id,
        browser_id=browser_id,
        device_id=device_id,
        client_version=client_version,
        user_agent=user_agent,
        extras=extras,
    )
    save_notion_account(acc, account_path)
    return acc


async def bootstrap_from_cookie(
    cookie: str,
    *,
    space_name: str | None = None,
    account_path: str = "notion_account.json",
    user_agent: str | None = None,
    client_version: str | None = None,
) -> NotionAccount:
    return await asyncio.to_thread(
        bootstrap_from_cookie_sync,
        cookie,
        space_name=space_name,
        account_path=account_path,
        user_agent=user_agent,
        client_version=client_version,
    )
