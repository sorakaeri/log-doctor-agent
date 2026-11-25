"""[Workflow 2단계] 에러 감지 + 중복 묶기 (LLM 없이 코드로 고정 처리).

- 새로 들어온 로그 중 에러만 골라낸다.
- 숫자·IP·시간처럼 매번 달라지는 부분을 지워 '에러 지문(signature)'을 만들고,
  같은 지문끼리 묶어 하나의 장애(Incident)로 만든다.
- 같은 장애를 이미 알렸다면 cooldown 동안 다시 알리지 않는다. (알림 폭탄 방지)
"""

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from .collector import LogEvent

ERROR_LEVELS = {"ERROR", "CRITICAL"}


def signature(event: LogEvent) -> str:
    msg = event.message
    msg = re.sub(r"\*\d+ ", "", msg)                                   # nginx 연결 번호
    msg = re.sub(r"\d+#\d+: ", "", msg)                                # nginx pid#tid
    msg = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b", "<ip>", msg)     # IP:포트
    msg = re.sub(r"excess: [\d.]+", "excess: <n>", msg)
    msg = re.sub(r"\(\d+(\.\d+)?ms\)", "", msg)
    msg = re.sub(r"/\d+\b", "/<id>", msg)
    msg = re.sub(r", (client|server|request|upstream|host|referrer): .*$", "", msg)
    return f"{event.source}: {msg.strip()[:160]}"


def is_error(event: LogEvent) -> bool:
    if event.status is not None:
        return event.status >= 500 or event.status == 429
    return event.level in ERROR_LEVELS


@dataclass
class Incident:
    id: str
    events: list[LogEvent]
    signatures: Counter
    detected_at: float = field(default_factory=time.time)
    suppressed: int = 0          # cooldown 때문에 묶여서 알리지 않은 반복 횟수

    @property
    def primary(self) -> str:
        """중복 판단 기준: 로그 메시지 지문을 우선하고, 없으면 HTTP 상태 지문.
        (같은 DB 장애라도 주기마다 503 개수는 달라지므로 원인 메시지를 기준으로 삼는다)"""
        for sig, _ in self.signatures.most_common():
            if not sig.startswith("nginx_access"):
                return sig
        return self.signatures.most_common(1)[0][0]

    def summary_lines(self, limit: int = 8) -> list[str]:
        return [f"{n}회 | {sig}" for sig, n in self.signatures.most_common(limit)]

    def sample_logs(self, limit: int = 12) -> list[str]:
        """원본 로그 샘플: 지문별로 최대 2줄씩"""
        picked: list[str] = []
        per_sig: Counter = Counter()
        for e in self.events:
            sig = signature(e)
            if per_sig[sig] < 2:
                per_sig[sig] += 1
                picked.append(e.raw)
            if len(picked) >= limit:
                break
        return picked


class Detector:
    def __init__(self, min_status_events: int, cooldown_sec: int):
        self.min_status_events = min_status_events
        self.cooldown_sec = cooldown_sec
        self.last_notified: dict[str, float] = {}
        self.suppressed: Counter = Counter()

    def detect(self, events: list[LogEvent]) -> Incident | None:
        errors = [e for e in events if is_error(e)]
        if not errors:
            return None

        sigs = Counter(signature(e) for e in errors)
        # HTTP 상태 코드 이벤트는 몇 건 이상일 때만 의미 있는 장애로 본다
        status_total = sum(n for s, n in sigs.items() if s.startswith("nginx_access"))
        log_errors = sum(n for s, n in sigs.items() if not s.startswith("nginx_access"))
        if log_errors == 0 and status_total < self.min_status_events:
            return None

        incident = Incident(
            id=hashlib.sha1(",".join(sorted(sigs)).encode()).hexdigest()[:8],
            events=errors,
            signatures=sigs,
        )

        key = incident.primary
        now = time.time()
        if now - self.last_notified.get(key, 0) < self.cooldown_sec:
            self.suppressed[key] += len(errors)
            return None
        incident.suppressed = self.suppressed.pop(key, 0)
        self.last_notified[key] = now
        return incident
