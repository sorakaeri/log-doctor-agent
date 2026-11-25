"""[Workflow 3단계] 에러 유형 분류 (Routing).

LLM이 장애를 4가지 유형 중 하나로 분류하고, 그 결과에 따라
다음 단계(Agent 조사)에서 쓸 도구와 조사 지침이 달라진다.
LLM을 쓸 수 없을 때는 키워드 규칙으로 분류한다. (Fallback)
"""

import json
import re
from dataclasses import dataclass

from .detector import Incident
from .llm import GeminiClient, LLMError, parse_json, text_of

CATEGORIES = {
    "DB_CONNECTION": "DB 연결 장애",
    "NETWORK": "네트워크/업스트림 연결 장애",
    "RESOURCE": "리소스 부족/트래픽 폭주",
    "PERMISSION": "권한 오류",
    "UNKNOWN": "분류 불가",
}
SEVERITIES = ("critical", "warning", "info")

SYSTEM_PROMPT = f"""너는 서버 장애를 분류하는 SRE 엔지니어다.
대상 시스템: Nginx(웹) → FastAPI(WAS, 컨테이너 ld-app) → PostgreSQL(DB, 컨테이너 ld-db), Docker Compose.
주어진 에러 로그 요약을 보고 장애 유형을 아래 중 하나로 고른다.
{json.dumps(CATEGORIES, ensure_ascii=False, indent=1)}
심각도: critical(서비스 불가), warning(일부 실패/성능 저하), info(무시 가능).
반드시 JSON으로만 답한다:
{{"category": "...", "severity": "...", "title": "한 줄 제목(한국어, 30자 이내)", "reason": "분류 근거 한 문장"}}"""

# Fallback 규칙: (정규식, 유형, 심각도, 제목)
RULES = [
    (r"DB 연결 실패|psycopg|could not connect|connection to server|5432", "DB_CONNECTION", "critical", "DB 연결 장애"),
    (r"permission denied|EACCES|forbidden|-> 403", "PERMISSION", "warning", "권한 오류"),
    (r"limiting requests|-> 429|out of memory|OOM|No space left", "RESOURCE", "warning", "요청 폭주/리소스 부족"),
    (r"connect\(\) failed|could not be resolved|upstream|-> 502|-> 504|Connection refused", "NETWORK", "critical", "업스트림(WAS) 연결 장애"),
]


@dataclass
class Classification:
    category: str
    severity: str
    title: str
    reason: str
    by: str  # "llm" 또는 "rule"

    @property
    def label(self) -> str:
        return CATEGORIES.get(self.category, self.category)


def classify_by_rules(incident: Incident) -> Classification:
    text = "\n".join(incident.signatures)
    # 규칙 순서 = 우선순위 (DB 원인이 있으면 503보다 DB 장애로 본다)
    for pattern, category, severity, title in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return Classification(category, severity, title, f"키워드 규칙 일치: {pattern}", "rule")
    return Classification("UNKNOWN", "warning", "원인 미상 에러", "일치하는 규칙 없음", "rule")


def classify(incident: Incident, llm: GeminiClient) -> Classification:
    if not llm.available:
        return classify_by_rules(incident)

    prompt = "에러 요약 (발생 횟수 | 지문):\n" + "\n".join(incident.summary_lines())
    prompt += "\n\n원본 로그 샘플:\n" + "\n".join(incident.sample_logs(6))
    try:
        content = llm.generate(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system=SYSTEM_PROMPT,
            json_mode=True,
        )
        d = parse_json(text_of(content))
        category = d.get("category", "UNKNOWN")
        if category not in CATEGORIES:
            category = "UNKNOWN"
        severity = d.get("severity", "warning")
        if severity not in SEVERITIES:
            severity = "warning"
        return Classification(category, severity, d.get("title", CATEGORIES[category]), d.get("reason", ""), "llm")
    except (LLMError, ValueError) as e:
        print(f"  ! LLM 분류 실패 → 규칙 기반으로 대체: {e}")
        return classify_by_rules(incident)
