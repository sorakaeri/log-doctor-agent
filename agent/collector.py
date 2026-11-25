"""[Workflow 1단계] 로그 수집.

각 로그 파일에서 '지난번에 읽은 위치(offset) 이후'에 추가된 줄만 읽어
공통 형식(LogEvent)으로 바꾼다. 읽은 위치는 .state.json에 저장한다.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

NGINX_ERROR_RE = re.compile(
    r"^(?P<time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>\w+)\] (?P<msg>.*)$"
)
APP_RE = re.compile(r"^(?P<time>\S+) (?P<level>[A-Z]+) \[app\] (?P<msg>.*)$")


@dataclass
class LogEvent:
    source: str          # nginx_access / nginx_error / app
    time: str
    level: str           # INFO / WARNING / ERROR ...
    message: str
    status: int | None = None   # HTTP 상태 코드 (access 로그만)
    raw: str = ""


def parse_line(source: str, line: str) -> LogEvent | None:
    line = line.rstrip("\r\n")
    if not line.strip():
        return None

    if source == "nginx_access":
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        status = int(d.get("status", 0))
        level = "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO"
        msg = f'{d.get("method")} {d.get("uri")} -> {status} (upstream={d.get("upstream_status") or "-"})'
        return LogEvent(source, d.get("time", ""), level, msg, status, line)

    if source == "nginx_error":
        m = NGINX_ERROR_RE.match(line)
        if not m:
            return LogEvent(source, "", "INFO", line, raw=line)
        level = m["level"].upper()
        level = {"WARN": "WARNING", "CRIT": "CRITICAL", "EMERG": "CRITICAL", "ALERT": "CRITICAL"}.get(level, level)
        return LogEvent(source, m["time"], level, m["msg"], raw=line)

    if source == "app":
        m = APP_RE.match(line)
        if not m:
            # 여러 줄 예외(traceback)의 나머지 줄 등
            return LogEvent(source, "", "INFO", line, raw=line)
        return LogEvent(source, m["time"], m["level"], m["msg"], raw=line)

    return None


class LogCollector:
    def __init__(self, files: dict[str, Path], state_file: Path):
        self.files = files
        self.state_file = state_file
        self.offsets: dict[str, int] = self._load_state()

    def _load_state(self) -> dict[str, int]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.offsets), encoding="utf-8")

    def skip_to_end(self) -> None:
        """기존 로그는 무시하고 지금부터 쌓이는 로그만 보도록 위치를 끝으로 옮긴다."""
        for source, path in self.files.items():
            self.offsets[source] = path.stat().st_size if path.exists() else 0
        self._save_state()

    def collect(self) -> list[LogEvent]:
        events: list[LogEvent] = []
        for source, path in self.files.items():
            if not path.exists():
                continue
            size = path.stat().st_size
            offset = self.offsets.get(source, 0)
            if size < offset:       # 파일이 새로 만들어졌거나 비워짐 → 처음부터
                offset = 0
            if size == offset:
                continue
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
            # 마지막 줄이 아직 쓰는 중일 수 있으므로 완성된 줄까지만 처리
            end = data.rfind(b"\n") + 1
            self.offsets[source] = offset + end
            for raw in data[:end].decode("utf-8", errors="replace").splitlines():
                event = parse_line(source, raw)
                if event:
                    events.append(event)
        self._save_state()
        return events
