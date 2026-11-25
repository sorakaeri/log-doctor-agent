"""[Workflow 마지막 단계] 결과를 Slack으로 전송 (Incoming Webhook, 단방향)."""

import json
import time
import urllib.error
import urllib.request

from .classifier import Classification
from .detector import Incident
from .investigator import Investigation

SEVERITY_EMOJI = {"critical": ":rotating_light:", "warning": ":warning:", "info": ":information_source:"}


def _bullets(items: list[str]) -> str:
    return "\n".join(f"• {i}" for i in items) or "-"


def build_message(incident: Incident, cls: Classification, inv: Investigation) -> dict:
    emoji = SEVERITY_EMOJI.get(cls.severity, ":warning:")
    detected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(incident.detected_at))
    total = len(incident.events)
    repeat = f" (+ 직전 알림 뒤 묶어 둔 반복 {incident.suppressed}건)" if incident.suppressed else ""
    steps = " → ".join(inv.steps) if inv.steps else "-"
    analyzed_by = "AI Agent" if inv.by == "agent" else "규칙 기반 (LLM 미사용)"

    text = f"{emoji} [{cls.severity.upper()}] {cls.title}"
    return {
        "text": text,  # 알림 미리보기용
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*유형*\n{cls.label}"},
                    {"type": "mrkdwn", "text": f"*감지 시각*\n{detected}"},
                    {"type": "mrkdwn", "text": f"*에러 로그*\n{total}건{repeat}"},
                    {"type": "mrkdwn", "text": f"*분석*\n{analyzed_by} · 신뢰도 {inv.confidence}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*원인 추정*\n{inv.root_cause}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*근거*\n{_bullets(inv.evidence)}"[:2900]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*조치 방안 (제안)*\n{_bullets(inv.actions)}"[:2900]}},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"조사 과정: {steps}"[:2900]},
                    {"type": "mrkdwn", "text": f"장애 ID `{incident.id}` · 분류: {cls.by} · Log Doctor는 읽기 전용이며 직접 조치하지 않습니다."},
                ],
            },
        ],
    }


def send_slack(webhook_url: str, message: dict) -> bool:
    if not webhook_url:
        print("  ! SLACK_WEBHOOK_URL이 없어 콘솔에만 출력합니다.")
        return False
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f"  ! Slack 전송 실패: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as e:
        print(f"  ! Slack 연결 실패: {e}")
    return False


def print_message(message: dict) -> None:
    print("  ┌ Slack 메시지 미리보기")
    for block in message["blocks"]:
        if block["type"] == "header":
            print("  │ " + block["text"]["text"])
        elif block["type"] == "section" and "fields" in block:
            print("  │ " + " | ".join(f["text"].replace("\n", ": ").replace("*", "") for f in block["fields"]))
        elif block["type"] == "section":
            for line in block["text"]["text"].replace("*", "").splitlines():
                print("  │ " + line)
        elif block["type"] == "context":
            for el in block["elements"]:
                print("  │ " + el["text"])
    print("  └")
