"""Log Doctor 실행 진입점.

전체 흐름 (한 주기):
  ① 로그 수집 → ② 에러 감지·중복 묶기 → ③ LLM 분류(Routing)
  → ④ Agent 원인 조사(Tool Calling) → ⑤ Slack 전송

사용법 (프로젝트 루트에서):
  python -m agent                # 30초마다 감시 (Ctrl+C로 종료)
  python -m agent --once         # 한 번만 확인하고 종료
  python -m agent --dry-run      # Slack에 보내지 않고 콘솔에만 출력
  python -m agent --from-start   # 기존 로그도 처음부터 분석
  python -m agent --test-slack   # Slack Webhook 연결 테스트
  python -m agent --check        # 설정·Gemini 연결 점검
"""

import argparse
import sys
import time

from .classifier import classify
from .collector import LogCollector
from .config import Config
from .detector import Detector
from .investigator import investigate
from .llm import GeminiClient, LLMError, text_of
from .reporter import build_message, print_message, send_slack
from .tools import Toolbox


def run_cycle(config, collector, detector, llm, tools, dry_run: bool) -> bool:
    """한 주기 실행. 장애를 알렸으면 True."""
    events = collector.collect()                                   # ①
    incident = detector.detect(events)                             # ②
    now = time.strftime("%H:%M:%S")
    if incident is None:
        errors = sum(1 for e in events if e.level in ("ERROR", "CRITICAL") or (e.status or 0) >= 500)
        note = f", 에러 {errors}건은 기존 장애와 같아 묶음" if errors else ""
        print(f"[{now}] 새 로그 {len(events)}줄, 신규 장애 없음{note}")
        return False

    print(f"[{now}] 장애 감지 (ID {incident.id}, 에러 {len(incident.events)}건)")
    for line in incident.summary_lines(3):
        print(f"    {line}")

    cls = classify(incident, llm)                                  # ③
    print(f"  분류: {cls.label} / {cls.severity} ({cls.by}) - {cls.reason}")
    if cls.severity == "info":
        print("  심각도 info → 알리지 않음")
        return False

    print("  Agent 조사 시작")
    inv = investigate(incident, cls, llm, tools, config.max_agent_steps)   # ④
    print(f"  원인: {inv.root_cause}")

    message = build_message(incident, cls, inv)                    # ⑤
    print_message(message)
    if not dry_run:
        ok = send_slack(config.slack_webhook_url, message)
        print("  Slack 전송 완료" if ok else "  Slack 전송 안 됨")
    print(f"  (이번 장애에 사용한 Gemini 호출: 누적 {llm.calls}회)")
    return True


def check(config: Config, llm: GeminiClient) -> None:
    print(f"로그 파일:")
    for name, path in config.log_files.items():
        print(f"  {name:<13} {'있음' if path.exists() else '없음'}  {path}")
    print(f"Slack Webhook: {'설정됨' if config.slack_webhook_url else '없음'}")
    print(f"Gemini 모델: {config.gemini_model}, API 키: {'설정됨' if llm.available else '없음'}")
    if llm.available:
        try:
            reply = llm.generate([{"role": "user", "parts": [{"text": "'연결 성공'이라고만 답해"}]}])
            print(f"  Gemini 응답: {text_of(reply).strip()}")
        except LLMError as e:
            print(f"  Gemini 오류: {e}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 깨짐 방지
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(prog="python -m agent", description="Log Doctor 장애 분석 Agent")
    parser.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    parser.add_argument("--dry-run", action="store_true", help="Slack에 보내지 않음")
    parser.add_argument("--from-start", action="store_true", help="기존 로그도 처음부터 분석")
    parser.add_argument("--test-slack", action="store_true", help="Slack Webhook 테스트 메시지 전송")
    parser.add_argument("--check", action="store_true", help="설정과 Gemini 연결 점검")
    args = parser.parse_args()

    config = Config.load()
    llm = GeminiClient(config.gemini_api_key, config.gemini_model)

    if args.check:
        check(config, llm)
        return
    if args.test_slack:
        ok = send_slack(config.slack_webhook_url, {"text": ":white_check_mark: Log Doctor 연결 테스트 메시지입니다."})
        print("Slack 전송 성공" if ok else "Slack 전송 실패")
        return

    collector = LogCollector(config.log_files, config.state_file)
    if args.from_start:
        collector.offsets = {}
    elif not args.once:
        collector.skip_to_end()   # 감시 모드는 '지금부터' 쌓이는 로그만 본다
    detector = Detector(config.min_status_events, config.cooldown_sec)
    tools = Toolbox(config)

    mode = "LLM + Agent" if llm.available else "규칙 기반 (GEMINI_API_KEY 없음)"
    print(f"Log Doctor 시작 | 모드: {mode} | 모델: {config.gemini_model} | 주기: {config.interval_sec}초")

    if args.once:
        run_cycle(config, collector, detector, llm, tools, args.dry_run)
        return
    try:
        while True:
            run_cycle(config, collector, detector, llm, tools, args.dry_run)
            time.sleep(config.interval_sec)
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
