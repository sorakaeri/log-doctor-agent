"""Agent가 사용할 수 있는 도구 (모두 읽기 전용).

로그 조회, 컨테이너 상태, 리소스 사용량, 엔드포인트 점검, 조치 문서 검색.
서버를 변경하는 명령(재시작, 삭제 등)은 일부러 만들지 않았다.
"""

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from .config import Config

MAX_OUTPUT = 3000  # 도구 결과를 LLM에 넘길 때 최대 글자 수 (토큰 절약)
SAFE_PATHS = ("/nginx-health", "/health", "/health/db", "/api/items")


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[-MAX_OUTPUT:] + "\n...(앞부분 생략)"


def _tail_lines(path: Path, max_bytes: int = 200_000) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", errors="replace").splitlines()


def _docker(*args: str) -> str:
    try:
        r = subprocess.run(
            ["docker", *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )
    except FileNotFoundError:
        return "docker 명령을 찾을 수 없음 (Docker Desktop 실행 여부 확인)"
    except subprocess.TimeoutExpired:
        return "docker 명령 시간 초과"
    out = (r.stdout + r.stderr).strip()
    return out or "(출력 없음)"


class Toolbox:
    def __init__(self, config: Config):
        self.config = config
        self.runbook_sections = self._load_runbooks()

    # ------------------------------------------------------------------ 도구
    def search_logs(self, source: str, keyword: str = "", limit: int = 20) -> str:
        path = self.config.log_files.get(source)
        if path is None:
            return f"알 수 없는 source: {source} (가능: {', '.join(self.config.log_files)})"
        lines = _tail_lines(path)
        if keyword:
            lines = [l for l in lines if keyword.lower() in l.lower()]
        limit = max(1, min(int(limit), 50))
        picked = lines[-limit:]
        if not picked:
            return f"{source}에서 '{keyword}'와 일치하는 로그 없음"
        return _truncate(f"[{source}] 최근 {len(picked)}줄 (전체 일치 {len(lines)}줄)\n" + "\n".join(picked))

    def get_container_status(self) -> str:
        fmt = "{{.Name}} state={{.State.Status}} exit_code={{.State.ExitCode}} oom_killed={{.State.OOMKilled}} restarts={{.RestartCount}} started={{.State.StartedAt}} finished={{.State.FinishedAt}}"
        out = _docker("inspect", "--format", fmt, *self.config.containers)
        return _truncate(out.replace("/ld-", "ld-"))

    def get_container_logs(self, container: str, tail: int = 30) -> str:
        if container not in self.config.containers:
            return f"허용되지 않은 컨테이너: {container} (가능: {', '.join(self.config.containers)})"
        tail = max(1, min(int(tail), 100))
        return _truncate(_docker("logs", "--tail", str(tail), container))

    def get_resource_usage(self) -> str:
        fmt = "{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} mem%={{.MemPerc}} net={{.NetIO}} pids={{.PIDs}}"
        return _truncate(_docker("stats", "--no-stream", "--format", fmt, *self.config.containers))

    def check_endpoint(self, path: str) -> str:
        if path not in SAFE_PATHS:
            return f"점검 가능한 경로가 아님: {path} (가능: {', '.join(SAFE_PATHS)})"
        try:
            with urllib.request.urlopen(self.config.base_url + path, timeout=5) as r:
                return f"GET {path} -> {r.status} {r.read(200).decode('utf-8', 'replace')}"
        except urllib.error.HTTPError as e:
            return f"GET {path} -> {e.code} {e.read(200).decode('utf-8', 'replace')}"
        except Exception as e:  # 연결 자체 실패
            return f"GET {path} -> 연결 실패 ({e})"

    def summarize_traffic(self, last_n: int = 500) -> str:
        lines = _tail_lines(self.config.log_files["nginx_access"])[-max(10, min(int(last_n), 5000)):]
        status, clients, uris = Counter(), Counter(), Counter()
        times = []
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            status[d.get("status")] += 1
            clients[d.get("remote_addr")] += 1
            uris[d.get("uri")] += 1
            times.append(d.get("time", ""))
        if not times:
            return "access 로그 없음"
        return (
            f"최근 요청 {len(times)}건 ({times[0]} ~ {times[-1]})\n"
            f"상태 코드: {dict(status.most_common())}\n"
            f"요청 많은 클라이언트: {clients.most_common(3)}\n"
            f"요청 많은 URI: {uris.most_common(3)}"
        )

    def search_runbook(self, query: str) -> str:
        """조치 문서 검색 (간단한 RAG: 키워드 겹침 점수로 상위 2개 섹션 반환)"""
        q = set(_tokens(query))
        scored = sorted(
            ((len(q & set(_tokens(title + " " + body))), name, title, body) for name, title, body in self.runbook_sections),
            reverse=True,
        )
        hits = [s for s in scored if s[0] > 0][:2]
        if not hits:
            return "관련 조치 문서 없음"
        return _truncate("\n\n".join(f"[{name} › {title}]\n{body.strip()}" for _, name, title, body in hits))

    # ------------------------------------------------------------------ 내부
    def _load_runbooks(self) -> list[tuple[str, str, str]]:
        sections = []
        for path in sorted(self.config.runbook_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for chunk in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
                title, _, body = chunk.partition("\n")
                sections.append((path.stem, title.strip(), body))
        return sections

    def call(self, name: str, args: dict) -> str:
        func = getattr(self, name, None)
        if name not in TOOL_DECLARATIONS or func is None:
            return f"알 수 없는 도구: {name}"
        try:
            return func(**args)
        except TypeError as e:
            return f"도구 인자 오류: {e}"
        except Exception as e:
            return f"도구 실행 오류: {e}"


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[0-9a-zA-Z가-힣_]+", text.lower()) if len(t) > 1]


# Gemini Function Calling용 함수 선언
TOOL_DECLARATIONS = {
    "search_logs": {
        "name": "search_logs",
        "description": "로그 파일에서 최근 줄을 키워드로 검색한다.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["nginx_access", "nginx_error", "app"], "description": "로그 종류"},
                "keyword": {"type": "string", "description": "포함해야 할 문자열 (비우면 전체)"},
                "limit": {"type": "integer", "description": "가져올 줄 수 (최대 50)"},
            },
            "required": ["source"],
        },
    },
    "get_container_status": {
        "name": "get_container_status",
        "description": "ld-nginx, ld-app, ld-db 컨테이너의 실행 상태, 종료 코드, OOM 여부, 재시작 횟수를 조회한다.",
    },
    "get_container_logs": {
        "name": "get_container_logs",
        "description": "컨테이너의 표준출력 로그(docker logs)를 조회한다. 컨테이너가 종료된 이유를 볼 때 유용하다.",
        "parameters": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "enum": ["ld-nginx", "ld-app", "ld-db"]},
                "tail": {"type": "integer", "description": "마지막 몇 줄 (최대 100)"},
            },
            "required": ["container"],
        },
    },
    "get_resource_usage": {
        "name": "get_resource_usage",
        "description": "실행 중인 컨테이너의 CPU, 메모리, 네트워크 사용량을 조회한다.",
    },
    "check_endpoint": {
        "name": "check_endpoint",
        "description": "서비스 엔드포인트에 GET 요청을 보내 현재 응답을 확인한다. /health는 WAS 자체, /health/db는 DB 연결까지 확인한다.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "enum": list(SAFE_PATHS)}},
            "required": ["path"],
        },
    },
    "summarize_traffic": {
        "name": "summarize_traffic",
        "description": "Nginx access 로그로 최근 요청의 상태 코드 분포, 요청이 많은 클라이언트/URI를 요약한다.",
        "parameters": {
            "type": "object",
            "properties": {"last_n": {"type": "integer", "description": "최근 몇 건을 볼지 (최대 5000)"}},
        },
    },
    "search_runbook": {
        "name": "search_runbook",
        "description": "장애 조치 문서(runbook)에서 관련 내용을 검색한다. 최종 조치 방안을 쓰기 전에 반드시 사용한다.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "검색어 (예: DB 연결 실패 조치)"}},
            "required": ["query"],
        },
    },
}

# Routing: 장애 유형별로 Agent에게 줄 도구와 조사 지침
ROUTES = {
    "DB_CONNECTION": {
        "tools": ["get_container_status", "get_container_logs", "check_endpoint", "search_logs", "search_runbook"],
        "guide": "DB 컨테이너(ld-db)가 살아 있는지, 앱이 DB에 접속하지 못하는 이유(중지/종료 코드/인증/네트워크)를 확인하라.",
    },
    "NETWORK": {
        "tools": ["get_container_status", "get_container_logs", "check_endpoint", "search_logs", "search_runbook"],
        "guide": "Nginx가 WAS(ld-app)에 연결하지 못하는 이유를 확인하라. WAS 컨테이너 상태와 종료 원인, Nginx error 로그를 본다.",
    },
    "RESOURCE": {
        "tools": ["summarize_traffic", "get_resource_usage", "get_container_status", "search_logs", "search_runbook"],
        "guide": "트래픽 급증인지 리소스(CPU/메모리) 부족인지 구분하라. 요청이 특정 클라이언트에 몰렸는지 확인한다.",
    },
    "PERMISSION": {
        "tools": ["search_logs", "get_container_logs", "get_container_status", "search_runbook"],
        "guide": "어떤 파일/리소스에 대한 권한 오류인지, 어느 컨테이너에서 발생했는지 확인하라.",
    },
    "UNKNOWN": {
        "tools": list(TOOL_DECLARATIONS),
        "guide": "가능한 도구를 사용해 원인을 좁혀라.",
    },
}
