"""[Agent 단계] 원인 조사.

분류 결과(Routing)에 맞는 도구만 LLM에게 주고,
LLM이 스스로 "어떤 도구를 어떤 순서로 쓸지" 결정하게 한다.

  생각 → 도구 호출(functionCall) → 결과 확인(functionResponse) → 다시 생각 ... → 최종 보고(JSON)

최대 반복 횟수를 넘기면 그때까지 모은 정보로 마무리하게 한다.
"""

import json
from dataclasses import dataclass, field

from .classifier import Classification
from .detector import Incident
from .llm import GeminiClient, LLMError, function_calls, parse_json, text_of
from .tools import ROUTES, TOOL_DECLARATIONS, Toolbox

SYSTEM_PROMPT = """너는 Docker Compose 3-Tier 서비스(Nginx → FastAPI(ld-app) → PostgreSQL(ld-db))의 장애를 조사하는 SRE Agent다.
규칙:
- 너는 읽기 전용이다. 제공된 도구로 조회만 하고, 직접 고치지 않는다. 조치는 사람이 실행할 수 있게 '제안'한다.
- 추측하지 말고 도구로 확인한 사실을 근거로 삼는다. 필요한 도구만 골라 쓰고, 같은 도구를 같은 인자로 반복하지 않는다.
- 조치 방안을 쓰기 전에 search_runbook으로 조치 문서를 확인한다.
- 조치 방안은 사람이 터미널에서 직접 실행할 수 있는 명령으로 쓴다. 네가 쓰는 도구 이름(search_logs, summarize_traffic 등)은 사람이 실행할 수 없으므로 조치에 쓰지 않는다.
- 조치 방안은 실행 순서대로 쓰고, 마지막에는 복구됐는지 확인하는 방법(예: /health/db 응답 확인)을 넣는다.
- 컨테이너 종료 코드는 의미까지 해석해 근거에 쓴다. (예: 137 + OOMKilled=false → docker stop 등 외부 중지 가능성, OOMKilled=true → 메모리 부족, 1 → 앱 오류. 조치 문서 참고)
- 컨테이너 로그의 시간은 UTC다. 근거에 시간을 쓸 때는 한국 시간(KST, UTC+9)으로 바꿔 쓴다.
- 조사가 끝나면 도구를 더 부르지 말고 아래 JSON 하나로만 답한다. (한국어)
{"root_cause": "원인 한두 문장", "evidence": ["확인한 근거1", "근거2"], "actions": ["조치1 (명령어 포함)", "조치2"], "confidence": "high|medium|low"}"""


@dataclass
class Investigation:
    root_cause: str
    evidence: list[str]
    actions: list[str]
    confidence: str
    steps: list[str] = field(default_factory=list)  # 조사 과정 (도구 호출 기록)
    by: str = "agent"


def _format_args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def investigate(incident: Incident, cls: Classification, llm: GeminiClient, tools: Toolbox, max_steps: int) -> Investigation:
    if not llm.available:
        return fallback_investigation(incident, cls, tools, reason="LLM 미설정")

    route = ROUTES.get(cls.category, ROUTES["UNKNOWN"])
    declarations = [TOOL_DECLARATIONS[name] for name in route["tools"]]

    prompt = (
        f"장애 유형: {cls.label} ({cls.category}), 심각도: {cls.severity}\n"
        f"조사 지침: {route['guide']}\n\n"
        "에러 요약 (발생 횟수 | 지문):\n" + "\n".join(incident.summary_lines()) +
        "\n\n원본 로그 샘플:\n" + "\n".join(incident.sample_logs(8))
    )
    contents: list[dict] = [{"role": "user", "parts": [{"text": prompt}]}]
    steps: list[str] = []

    try:
        for step in range(max_steps + 1):
            last_step = step == max_steps
            if last_step:
                contents.append({"role": "user", "parts": [{"text": "도구 사용 횟수를 모두 썼다. 지금까지 확인한 내용으로 최종 JSON만 답하라."}]})
            content = llm.generate(contents, system=SYSTEM_PROMPT, tools=None if last_step else declarations)
            contents.append(content)  # 모델 응답은 그대로 대화 기록에 붙인다

            calls = function_calls(content)
            if not calls:
                d = parse_json(text_of(content))
                return Investigation(
                    root_cause=d.get("root_cause", "원인 미상"),
                    evidence=list(d.get("evidence", []))[:6],
                    actions=list(d.get("actions", []))[:6],
                    confidence=d.get("confidence", "medium"),
                    steps=steps,
                )

            responses = []
            for call in calls:
                name, args = call.get("name", ""), call.get("args", {}) or {}
                if name not in route["tools"]:
                    result = f"이 장애 유형에서는 사용할 수 없는 도구: {name}"
                else:
                    result = tools.call(name, args)
                steps.append(f"{name}({_format_args(args)})")
                print(f"    → {name}({_format_args(args)})")
                responses.append({"functionResponse": {"name": name, "response": {"result": result}}})
            contents.append({"role": "user", "parts": responses})
    except (LLMError, ValueError, json.JSONDecodeError) as e:
        print(f"  ! Agent 조사 실패 → 규칙 기반 보고로 대체: {e}")
        result = fallback_investigation(incident, cls, tools, reason=str(e)[:100])
        result.steps = steps + result.steps
        return result

    return fallback_investigation(incident, cls, tools, reason="최대 반복 초과")


# 기본 조치 (LLM을 쓸 수 없을 때)
DEFAULT_ACTIONS = {
    "DB_CONNECTION": ["`docker compose ps db`로 DB 상태 확인", "중지된 경우 `docker compose start db`", "`docker logs ld-db --tail 50`로 종료 원인 확인"],
    "NETWORK": ["`docker compose ps app`로 WAS 상태 확인", "중지된 경우 `docker compose start app`", "`docker logs ld-app --tail 50`로 종료 원인 확인"],
    "RESOURCE": ["`docker stats`로 CPU/메모리 확인", "특정 클라이언트 요청 폭주라면 차단 또는 rate limit 조정", "정상 트래픽 증가라면 WAS 리소스/인스턴스 확장 검토"],
    "PERMISSION": ["오류 로그에서 대상 파일/경로 확인", "컨테이너 실행 사용자와 볼륨 권한 확인"],
    "UNKNOWN": ["에러 로그 원문을 확인하고 담당자 점검 필요"],
}


def fallback_investigation(incident: Incident, cls: Classification, tools: Toolbox, reason: str) -> Investigation:
    """LLM 없이 고정 절차로 상태만 모아서 보고한다. (Workflow만으로 동작하는 안전장치)"""
    status = tools.get_container_status()
    return Investigation(
        root_cause=f"{cls.label} 추정 (규칙 기반 분류, 사유: {reason})",
        evidence=[incident.summary_lines(1)[0], "컨테이너 상태: " + status.replace("\n", " / ")[:300]],
        actions=DEFAULT_ACTIONS.get(cls.category, DEFAULT_ACTIONS["UNKNOWN"]),
        confidence="low",
        steps=["get_container_status()"],
        by="fallback",
    )
