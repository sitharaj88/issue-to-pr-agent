from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from ...domain.entities import NotificationEventType, NotificationRecord, NotificationStatus
from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository
from ...integrations.jira.client import JiraClient
from ...integrations.slack.client import SlackWebhookClient
from ...integrations.teams.client import TeamsWebhookClient


class FileNotificationOutbox:
    def __init__(
        self,
        repository: RunRepository,
        *,
        settings: Settings | None = None,
        slack_client: SlackWebhookClient | None = None,
        teams_client: TeamsWebhookClient | None = None,
        jira_client: JiraClient | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._slack_client = slack_client or (SlackWebhookClient(settings) if settings is not None else None)
        self._teams_client = teams_client or (TeamsWebhookClient(settings) if settings is not None else None)
        self._jira_client = jira_client or (JiraClient(settings) if settings is not None else None)

    def emit(
        self,
        *,
        tenant_id: str,
        event_type: NotificationEventType,
        summary: str,
        payload: dict[str, object],
        output_dir: Path,
    ) -> NotificationRecord:
        notification_id = uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        destination = output_dir / tenant_id / f"{notification_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "notification_id": notification_id,
            "created_at": created_at,
            "tenant_id": tenant_id,
            "event_type": event_type.value,
            "status": NotificationStatus.EMITTED.value,
            "summary": summary,
            "payload": payload,
            "dispatch_results": self._dispatch_external(
                event_type=event_type,
                summary=summary,
                payload=payload,
            ),
        }
        destination.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record = NotificationRecord(
            notification_id=notification_id,
            created_at=created_at,
            tenant_id=tenant_id,
            event_type=event_type,
            status=NotificationStatus.EMITTED,
            summary=summary,
            payload_path=destination,
        )
        self._repository.save_notification(
            record,
            {
                **body,
                "payload_path": str(destination),
            },
        )
        return record

    def _dispatch_external(
        self,
        *,
        event_type: NotificationEventType,
        summary: str,
        payload: dict[str, object],
    ) -> list[dict[str, object]]:
        if self._settings is None:
            return []
        results: list[dict[str, object]] = []
        if self._slack_client is not None and self._settings.slack_webhook_url:
            results.append(
                self._dispatch_channel(
                    destination="slack",
                    callback=lambda: self._slack_client.send_event(
                        event_type=event_type.value,
                        summary=summary,
                        payload=payload,
                    ),
                )
            )
        if self._teams_client is not None and self._settings.teams_webhook_url:
            results.append(
                self._dispatch_channel(
                    destination="teams",
                    callback=lambda: self._teams_client.send_event(
                        event_type=event_type.value,
                        summary=summary,
                        payload=payload,
                    ),
                )
            )
        external_ticket = self._resolve_external_ticket(payload)
        jira_key = _string_value(external_ticket.get("key"))
        if (
            self._jira_client is not None
            and jira_key
            and _string_value(external_ticket.get("system")).lower() == "jira"
            and self._settings.jira_base_url
            and self._settings.jira_token
        ):
            results.append(
                self._dispatch_channel(
                    destination="jira",
                    callback=lambda: self._jira_client.add_comment(
                        jira_key,
                        _jira_comment_body(event_type=event_type, summary=summary, payload=payload),
                    ),
                    metadata={"issue_key": jira_key},
                )
            )
        return results

    def _dispatch_channel(
        self,
        *,
        destination: str,
        callback,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        result = {"destination": destination, "status": "sent"}
        if metadata:
            result.update(metadata)
        try:
            callback()
        except Exception as exc:
            result["status"] = "failed"
            result["error_message"] = str(exc)
        return result

    def _resolve_external_ticket(self, payload: dict[str, object]) -> dict[str, object]:
        direct = payload.get("external_ticket")
        if isinstance(direct, dict):
            return direct

        run_id = _string_value(payload.get("run_id")) or _string_value(payload.get("linked_run_id"))
        if not run_id:
            approval_id = _string_value(payload.get("approval_id"))
            if approval_id:
                approval = self._repository.get_approval(approval_id)
                if approval is not None:
                    approval_record, _ = approval
                    run_id = approval_record.linked_run_id
        if not run_id:
            delivery_id = _string_value(payload.get("delivery_id"))
            if delivery_id:
                delivery = self._repository.get_delivery(delivery_id)
                if delivery is not None:
                    delivery_record, _ = delivery
                    run_id = delivery_record.linked_run_id
        if not run_id:
            return {}

        run = self._repository.get_run(run_id)
        if run is None:
            return {}
        _, run_payload = run
        external_ticket = run_payload.get("external_ticket")
        return external_ticket if isinstance(external_ticket, dict) else {}


def _jira_comment_body(
    *,
    event_type: NotificationEventType,
    summary: str,
    payload: dict[str, object],
) -> str:
    lines = [
        f"Issue-to-PR event: {event_type.value}",
        "",
        summary,
    ]
    for key in ("repo_full_name", "run_id", "approval_id", "delivery_id", "status", "error_message"):
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
