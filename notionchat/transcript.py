from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from notionchat.account import NotionAccount


def _now_iso(tz: str | None = None) -> str:
    target: ZoneInfo | None = None
    if tz:
        try:
            target = ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            target = None
    return datetime.now(UTC).astimezone(target).isoformat(timespec="milliseconds")


def new_uuid() -> str:
    return str(uuid.uuid4())


def build_config_value(
    *,
    notion_model: str,
    is_subsequent_turn: bool = False,
    use_web_search: bool = True,
    use_workspace_search: bool = True,
    use_read_only_mode: bool = False,
    ide_agent_mode: bool = False,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "type": "workflow",
        "modelFromUser": not is_subsequent_turn,
        "enableAgentAutomations": False,
        "enableAgentIntegrations": False,
        "enableCustomAgents": False,
        "enableExperimentalIntegrations": False,
        "enableAgentDiffs": False,
        "enableCsvAttachmentSupport": False,
        "showDatabaseAgentsDiscoverability": False,
        "enableAgentThreadTools": False,
        "enableCrdtOperations": False,
        "enableAgentCardCustomization": False,
        "enableSystemPromptAsPage": False,
        "enableUserSessionContext": False,
        "enableLargeToolResultComputerOffload": False,
        "enableScriptAgentAdvanced": False,
        "enableScriptAgent": False,
        "enableScriptAgentSearchConnectorsInCustomAgent": False,
        "enableScriptAgentGoogleDriveInCustomAgent": False,
        "enableScriptAgentGoogleDriveOAuthInCustomAgent": False,
        "enableScriptAgentSlack": False,
        "enableScriptAgentMcpServers": False,
        "enableScriptAgentGtm": False,
        "enableScriptAgentCustomToolCalling": False,
        "enableComputer": False,
        "enableCreateAndRunThread": False,
        "enableSoftwareFactoryPage": False,
        "enableAgentGenerateImage": False,
        "enableSpeculativeSearch": False,
        "enableQueryCalendar": False,
        "enableQueryMail": False,
        "enableMailExplicitToolCalls": False,
        "enableMailNotificationPreferences": False,
        "enableMailAgentMultiProviderSupport": False,
        "useRulePrioritization": True,
        "availableConnectors": [],
        "customConnectorInfo": [],
        "searchScopes": [{"type": "everything"}],
        "useWebSearch": use_web_search,
        "isHipaa": False,
        "internetAccess": False,
        "useReadOnlyMode": use_read_only_mode,
        "writerMode": False,
        "isCustomAgent": False,
        "model": notion_model,
        "isCustomAgentBuilder": False,
        "isAgentResearchRequest": False,
        "useCustomAgentDraft": False,
        "use_draft_actor_pointer": False,
        "enableUpdatePageAutofixer": False,
        "enableMarkdownVNext": False,
        "enableEmbedBlocks": False,
        "updatePageStaleViewGuardEnabled": False,
        "enableUpdatePageOrderUpdates": False,
        "enableAgentSupportPropertyReorder": False,
        "agentShortUpdatePageResult": False,
        "enableAgentAskSurvey": False,
        "databaseAgentConfigMode": False,
        "isOnboardingAgent": False,
        "isMobile": False,
    }
    if not use_workspace_search and not use_web_search:
        cfg.pop("searchScopes", None)
    if ide_agent_mode:
        cfg["useWebSearch"] = False
        cfg["enableAgentThreadTools"] = False
        if cfg.get("searchScopes"):
            cfg["searchScopes"] = [{"type": "workspace"}]
    if is_subsequent_turn:
        cfg["isThreadStartedByAdmin"] = True
    return cfg


def build_context_value(
    acc: NotionAccount,
    *,
    current_datetime: str | None = None,
) -> dict[str, Any]:
    return {
        "timezone": acc.timezone,
        "userName": acc.user_name,
        "userId": acc.user_id,
        "userEmail": acc.user_email,
        "spaceName": acc.space_name,
        "spaceId": acc.space_id,
        "spaceViewId": acc.space_view_id,
        "currentDatetime": current_datetime or _now_iso(acc.timezone),
        "surface": "ai_module",
    }


def build_full_transcript(
    acc: NotionAccount,
    *,
    user_text: str,
    notion_model: str,
    config_id: str | None = None,
    context_id: str | None = None,
    now: str | None = None,
    ide_agent_mode: bool = False,
) -> list[dict[str, Any]]:
    now = now or _now_iso(acc.timezone)
    return [
        {
            "id": config_id or new_uuid(),
            "type": "config",
            "value": build_config_value(
                notion_model=notion_model,
                ide_agent_mode=ide_agent_mode,
            ),
        },
        {
            "id": context_id or new_uuid(),
            "type": "context",
            "value": build_context_value(acc, current_datetime=now),
        },
        {
            "id": new_uuid(),
            "type": "user",
            "value": [[user_text]],
            "userId": acc.user_id,
            "createdAt": now,
        },
    ]


def build_partial_transcript(
    acc: NotionAccount,
    *,
    new_user_text: str,
    notion_model: str,
    config_id: str,
    context_id: str,
    updated_config_ids: list[str],
    original_datetime: str | None = None,
    ide_agent_mode: bool = False,
) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = [
        {
            "id": config_id,
            "type": "config",
            "value": build_config_value(
                notion_model=notion_model,
                is_subsequent_turn=True,
                ide_agent_mode=ide_agent_mode,
            ),
        },
        {
            "id": context_id,
            "type": "context",
            "value": build_context_value(acc, current_datetime=original_datetime),
        },
    ]
    for uc_id in updated_config_ids:
        transcript.append({"id": uc_id, "type": "updated-config"})
    transcript.append(
        {
            "id": new_uuid(),
            "type": "user",
            "value": [[new_user_text]],
            "userId": acc.user_id,
            "createdAt": _now_iso(acc.timezone),
        }
    )
    return transcript


def build_inference_request(
    acc: NotionAccount,
    *,
    transcript: list[dict[str, Any]],
    thread_id: str,
    create_thread: bool,
    is_partial_transcript: bool,
    trace_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "traceId": trace_id or new_uuid(),
        "spaceId": acc.space_id,
        "transcript": transcript,
        "threadId": thread_id,
        "createThread": create_thread,
        "isPartialTranscript": is_partial_transcript,
        "generateTitle": False,
        "saveAllThreadOperations": False,
        "setUnreadState": False,
        "threadType": "workflow",
        "asPatchResponse": True,
        "patchResponseVersion": 2,
        "hasHeartbeat": False,
        "createdSource": "ai_module",
        "isUserInAnySalesAssistedSpace": False,
        "isSpaceSalesAssisted": False,
        "debugOverrides": {
            "emitAgentSearchExtractedResults": True,
            "cachedInferences": {},
            "annotationInferences": {},
            "emitInferences": False,
        },
    }
    if create_thread:
        body["threadParentPointer"] = {
            "table": "space",
            "id": acc.space_id,
            "spaceId": acc.space_id,
        }
    return body
