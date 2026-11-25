"""설정 로드: 프로젝트 루트의 .env 파일 + 환경 변수.

외부 패키지(python-dotenv) 없이 동작하도록 .env를 직접 읽는다.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE 형식의 .env를 읽어 os.environ에 넣는다.

    - 같은 키가 여러 번 나오면 마지막 값을 쓴다. (빈 줄이 위에 남아 있어도 아래 값이 적용됨)
    - 시스템 환경 변수에 값이 이미 있으면 그 값을 유지한다. (빈 값은 덮어씀)
    - Windows 메모장이 붙이는 BOM(utf-8-sig)도 처리한다.
    """
    if not path.exists():
        return
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        if not os.environ.get(key):
            os.environ[key] = value


@dataclass
class Config:
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    slack_webhook_url: str = ""

    interval_sec: int = 30          # 로그 확인 주기
    cooldown_sec: int = 300         # 같은 장애를 다시 알리기까지 대기 시간
    max_agent_steps: int = 6        # Agent의 도구 호출 최대 반복 횟수
    min_status_events: int = 3      # 5xx/429 응답이 이 횟수 이상일 때만 장애로 판단

    log_files: dict = field(default_factory=dict)
    state_file: Path = ROOT / "agent" / ".state.json"
    runbook_dir: Path = ROOT / "agent" / "runbooks"
    base_url: str = "http://localhost:8080"
    containers: tuple = ("ld-nginx", "ld-app", "ld-db")

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        logs = ROOT / "logs"
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            interval_sec=int(os.getenv("AGENT_INTERVAL_SEC", "30")),
            cooldown_sec=int(os.getenv("AGENT_COOLDOWN_SEC", "300")),
            log_files={
                "nginx_access": logs / "nginx" / "access.log",
                "nginx_error": logs / "nginx" / "error.log",
                "app": logs / "app" / "app.log",
            },
        )
