"""장애 시나리오 실행 스크립트 (Windows/Mac/Linux 공통, 표준 라이브러리만 사용).

사용법 (프로젝트 루트에서):
    python scripts/chaos.py status            # 각 계층 상태 확인
    python scripts/chaos.py traffic [초]      # 정상 트래픽 발생 (기본 20초)
    python scripts/chaos.py db-down           # 시나리오 1: DB 중지 → 앱 503
    python scripts/chaos.py app-down          # 시나리오 2: WAS 중지 → Nginx 502
    python scripts/chaos.py flood [초]        # 시나리오 3: 요청 폭주 → Nginx 429
    python scripts/chaos.py recover           # 모든 컨테이너 복구
"""

import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8080"
ENDPOINTS = ["/api/items", "/api/items/1", "/health/db"]


def compose(*args: str) -> None:
    cmd = ["docker", "compose", *args]
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=False)


def request(path: str) -> int:
    """GET 요청 후 상태 코드 반환. 연결 자체가 안 되면 0."""
    try:
        with urllib.request.urlopen(BASE_URL + path, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0


def send_requests(count: int, interval: float = 0.5) -> dict[int, int]:
    """여러 엔드포인트에 요청을 보내고 상태 코드별 개수를 집계한다."""
    stats: dict[int, int] = {}
    for i in range(count):
        code = request(ENDPOINTS[i % len(ENDPOINTS)])
        stats[code] = stats.get(code, 0) + 1
        time.sleep(interval)
    return stats


def print_stats(title: str, stats: dict[int, int]) -> None:
    summary = ", ".join(f"{code}: {n}건" for code, n in sorted(stats.items()))
    print(f"[{title}] {summary}")


def cmd_status() -> None:
    compose("ps")
    for path in ["/nginx-health", "/health", "/health/db"]:
        print(f"  {path:<15} -> {request(path) or '연결 실패'}")


def cmd_traffic(seconds: int = 20) -> None:
    print(f"정상 트래픽 {seconds}초 발생")
    print_stats("traffic", send_requests(seconds * 2))


def ensure_healthy() -> None:
    """장애를 내기 전에 서비스가 정상인지 확인한다.
    이전 시나리오를 복구하지 않고 다음 시나리오를 실행하면 결과가 섞이기 때문이다."""
    code = request("/health/db")
    if code != 200:
        print(f"! 서비스가 정상 상태가 아닙니다 (/health/db -> {code or '연결 실패'}).")
        print("  이전 장애가 남아 있으면 결과가 섞입니다. 먼저 실행하세요: python scripts/chaos.py recover")
        sys.exit(1)


def cmd_db_down() -> None:
    ensure_healthy()
    print("시나리오 1: DB 다운")
    compose("stop", "db")
    print_stats("db-down", send_requests(10))
    print("→ 기대 결과: 앱 로그에 'DB 연결 실패', Nginx access 로그에 503")


def cmd_app_down() -> None:
    ensure_healthy()
    print("시나리오 2: WAS 다운")
    compose("stop", "app")
    print_stats("app-down", send_requests(10))
    print("→ 기대 결과: Nginx error 로그에 upstream 연결 실패, access 로그에 502")


def cmd_flood(seconds: int = 10) -> None:
    ensure_healthy()
    print(f"시나리오 3: 요청 폭주 {seconds}초 (스레드 20개)")
    stats: dict[int, int] = {}
    lock = threading.Lock()
    deadline = time.time() + seconds

    def worker() -> None:
        while time.time() < deadline:
            code = request("/api/items")
            with lock:
                stats[code] = stats.get(code, 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print_stats("flood", stats)
    print("→ 기대 결과: Nginx access 로그에 429 다수, error 로그에 'limiting requests'")


def cmd_recover() -> None:
    print("전체 복구")
    compose("start", "db")
    time.sleep(5)  # DB 기동 대기
    compose("start", "app")
    time.sleep(3)
    cmd_status()


COMMANDS = {
    "status": cmd_status,
    "traffic": cmd_traffic,
    "db-down": cmd_db_down,
    "app-down": cmd_app_down,
    "flood": cmd_flood,
    "recover": cmd_recover,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    func = COMMANDS[sys.argv[1]]
    if len(sys.argv) > 2 and sys.argv[1] in ("traffic", "flood"):
        func(int(sys.argv[2]))
    else:
        func()


if __name__ == "__main__":
    main()
