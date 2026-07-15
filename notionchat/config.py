from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

from notionchat.account import (
    NotionAccount,
    load_notion_account,
    parse_browser_cookie,
    save_notion_account,
)
from notionchat.bootstrap import bootstrap_from_cookie_sync
from notionchat.browser_fp import DEFAULT_CLIENT_VERSION, DEFAULT_USER_AGENT
from notionchat.exceptions import NotionChatError

DEFAULT_BASE_URL = "https://app.notion.com/api/v3"


def _resolve_home() -> Path | None:
    """Project home for .env / account files when running `notion serve` from anywhere."""
    raw = os.getenv("NOTIONCHAT_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return None


def _load_dotenv_files() -> None:
    home = _resolve_home()
    if home is not None:
        load_dotenv(home / ".env", override=False)
    load_dotenv(override=False)


_load_dotenv_files()


@dataclass(slots=True)
class Settings:
    api_key: str
    host: str
    port: int
    account_path: Path
    thread_state_dir: Path
    base_url: str
    default_model: str


def _env_path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default)).expanduser()
    if not p.is_absolute():
        home = _resolve_home()
        if home is not None:
            return (home / p).resolve()
    return p


def load_settings() -> Settings:
    _load_dotenv_files()
    return Settings(
        api_key=os.getenv("NOTIONCHAT_API_KEY", "sk-notionchat"),
        host=os.getenv("NOTIONCHAT_HOST", "127.0.0.1"),
        port=int(os.getenv("NOTIONCHAT_PORT", "1994")),
        account_path=_env_path("NOTIONCHAT_ACCOUNT", "notion_account.json"),
        thread_state_dir=_env_path("NOTIONCHAT_THREADS_DIR", "threads"),
        base_url=os.getenv("NOTIONCHAT_NOTION_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        default_model=os.getenv("NOTIONCHAT_DEFAULT_MODEL", "ambrosia-tart-high"),
    )


def _cookie_identity_changed(acc: NotionAccount, cookie: str) -> bool:
    """True when the browser cookie belongs to a different session/workspace."""
    parsed = parse_browser_cookie(cookie)
    new_user = parsed.get("notion_user_id", "")
    new_token = parsed.get("token_v2", "")
    if new_user and acc.user_id and new_user != acc.user_id:
        return True
    if new_token and acc.token_v2 and new_token != acc.token_v2:
        return True
    if not acc.space_id or not acc.space_view_id:
        return True
    return False


def _apply_fingerprint_env(acc: NotionAccount) -> NotionAccount:
    ua = os.getenv("NOTION_USER_AGENT", "").strip()
    cv = os.getenv("NOTION_CLIENT_VERSION", "").strip()
    sec = os.getenv("NOTION_SEC_CH_UA", "").strip()
    extras = dict(acc.extras)
    if sec:
        extras["sec_ch_ua"] = sec
    return replace(
        acc,
        user_agent=ua or acc.user_agent or DEFAULT_USER_AGENT,
        client_version=cv or acc.client_version or DEFAULT_CLIENT_VERSION,
        extras=extras,
    )


def _refresh_cookie(acc: NotionAccount, cookie: str) -> NotionAccount:
    parsed = parse_browser_cookie(cookie)
    token = parsed.get("token_v2") or acc.token_v2
    acc = replace(
        acc,
        full_cookie=cookie.strip().rstrip(";"),
        token_v2=token,
        user_id=parsed.get("notion_user_id") or acc.user_id,
        browser_id=parsed.get("notion_browser_id") or acc.browser_id,
        device_id=parsed.get("device_id") or acc.device_id,
    )
    return _apply_fingerprint_env(acc)


def load_account_from_env(settings: Settings) -> NotionAccount:
    cookie = os.getenv("NOTION_COOKIE", "").strip()
    space_name = os.getenv("NOTION_SPACE_NAME", "").strip() or None
    account_path = str(settings.account_path)

    if cookie and settings.account_path.exists():
        acc = load_notion_account(settings.account_path)
        if _cookie_identity_changed(acc, cookie):
            return bootstrap_from_cookie_sync(
                cookie,
                space_name=space_name,
                account_path=account_path,
            )
        acc = _refresh_cookie(acc, cookie)
        save_notion_account(acc, settings.account_path)
        return acc

    if settings.account_path.exists():
        acc = load_notion_account(settings.account_path)
        if not acc.space_id:
            raise NotionChatError(
                "Account file is missing space_id. Run: python -m notionchat init --cookie \"...\"",
                status_code=500,
            )
        return _apply_fingerprint_env(acc)

    if cookie:
        return bootstrap_from_cookie_sync(
            cookie,
            space_name=space_name,
            account_path=account_path,
        )

    raise NotionChatError(
        "No Notion credentials found. Set NOTION_COOKIE in .env or run:\n"
        "  python -m notionchat init --cookie \"<paste document.cookie>\"",
        status_code=500,
    )
