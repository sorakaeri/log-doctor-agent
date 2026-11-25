"""전체 흐름 테스트 (Docker·Gemini·Slack 없이 실행 가능).

실제 장애 시나리오에서 나오는 형식의 로그를 임시 폴더에 쓰고,
가짜 LLM(FakeLLM)으로 감지 → 분류 → Agent 조사 → Slack 메시지 생성을 검증한다.

실행: python -m unittest -v
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent.classifier import classify, classify_by_rules
from agent.collector import LogCollector
from agent.config import ROOT, Config
from agent.detector import Detector
from agent.investigator import investigate
from agent.reporter import build_message
from agent.tools import Toolbox


def access(status: int, uri: str = "/api/items", upstream: str = "") -> str:
    return json.dumps({
        "time": "2026-11-25T10:00:00+00:00", "remote_addr": "172.18.0.1", "method": "GET",
        "uri": uri, "status": status, "request_time": 0.01,
        "upstream_status": upstream, "upstream_time": "0.010",
    })


SCENARIOS = {
    "db-down": {
        "app": [
            "2026-11-25T10:00:01+0000 ERROR [app] DB 연결 실패: connection is bad: [Errno -2] Name or service not known",
            "2026-11-25T10:00:01+0000 WARNING [app] GET /api/items -> 503 (3012.4ms)",
        ] * 4,
        "nginx_access": [access(503, upstream="503")] * 4,
        "nginx_error": [],
        "expect": "DB_CONNECTION",
    },
    "app-down": {
        "app": [],
        "nginx_access": [access(502)] * 5,
        "nginx_error": [
            '2026/11/25 10:00:02 [error] 29#29: *7 app could not be resolved (3: Host not found), client: 172.18.0.1, server: , request: "GET /api/items HTTP/1.1", host: "localhost:8080"',
        ] * 5,
        "expect": "NETWORK",
    },
    "flood": {
        "app": ["2026-11-25T10:00:03+0000 INFO [app] GET /api/items -> 200 (4.1ms)"] * 10,
        "nginx_access": [access(200, upstream="200")] * 30 + [access(429)] * 50,
        "nginx_error": [
            '2026/11/25 10:00:03 [error] 29#29: *120 limiting requests, excess: 20.950 by zone "api", client: 172.18.0.1, server: , request: "GET /api/items HTTP/1.1", host: "localhost:8080"',
        ] * 50,
        "expect": "RESOURCE",
    },
}


class FakeLLM:
    """분류 요청에는 JSON, 조사 요청에는 도구 호출 1번 → 최종 JSON 순서로 답한다."""

    def __init__(self, category: str):
        self.category = category
        self.calls = 0
        self.available = True
        self.seen_tools: list[list[str]] = []

    def generate(self, contents, system=None, tools=None, json_mode=False, retries=2):
        self.calls += 1
        if json_mode:  # 분류 단계
            return {"role": "model", "parts": [{"text": json.dumps({
                "category": self.category, "severity": "critical", "title": "테스트 장애", "reason": "테스트"})}]}
        self.seen_tools.append([t["name"] for t in tools or []])
        already_called = any("functionResponse" in p for c in contents for p in c.get("parts", []))
        if tools and not already_called:
            return {"role": "model", "parts": [
                {"functionCall": {"name": "get_container_status", "args": {}}},
                {"functionCall": {"name": "search_runbook", "args": {"query": "DB 연결 실패 조치"}}},
            ]}
        return {"role": "model", "parts": [{"text": "```json\n" + json.dumps({
            "root_cause": "원인 테스트", "evidence": ["근거"], "actions": ["조치"], "confidence": "high"}) + "\n```"}]}


class NoLLM:
    available = False
    calls = 0


def make_env(tmp: Path, logs: dict[str, list[str]]) -> Config:
    config = Config(
        log_files={
            "nginx_access": tmp / "access.log",
            "nginx_error": tmp / "error.log",
            "app": tmp / "app.log",
        },
        state_file=tmp / "state.json",
        runbook_dir=ROOT / "agent" / "runbooks",
        base_url="http://127.0.0.1:9",  # 아무것도 없는 포트 → 연결 실패 처리 확인
    )
    for source, path in config.log_files.items():
        path.write_text("\n".join(logs.get(source, [])) + "\n", encoding="utf-8")
    return config


class PipelineTest(unittest.TestCase):
    def run_scenario(self, name: str, llm):
        sc = SCENARIOS[name]
        with tempfile.TemporaryDirectory() as d:
            config = make_env(Path(d), sc)
            events = LogCollector(config.log_files, config.state_file).collect()
            incident = Detector(config.min_status_events, config.cooldown_sec).detect(events)
            self.assertIsNotNone(incident, f"{name}: 장애가 감지되어야 함")
            cls = classify(incident, llm)
            inv = investigate(incident, cls, llm, Toolbox(config), config.max_agent_steps)
            msg = build_message(incident, cls, inv)
            return incident, cls, inv, msg

    def test_rule_classification_for_each_scenario(self):
        for name, sc in SCENARIOS.items():
            with self.subTest(name):
                incident, cls, inv, msg = self.run_scenario(name, NoLLM())
                self.assertEqual(cls.category, sc["expect"])
                self.assertEqual(inv.by, "fallback")
                self.assertIn("blocks", msg)

    def test_agent_loop_with_tools(self):
        llm = FakeLLM("DB_CONNECTION")
        incident, cls, inv, msg = self.run_scenario("db-down", llm)
        self.assertEqual(cls.by, "llm")
        self.assertEqual(inv.by, "agent")
        self.assertEqual(inv.root_cause, "원인 테스트")
        self.assertEqual(len(inv.steps), 2)
        # Routing: DB 장애에는 트래픽 요약 도구를 주지 않는다
        self.assertNotIn("summarize_traffic", llm.seen_tools[0])
        self.assertEqual(llm.calls, 3)  # 분류 1 + 조사 2

    def test_dedup_within_cooldown(self):
        sc = SCENARIOS["db-down"]
        with tempfile.TemporaryDirectory() as d:
            config = make_env(Path(d), sc)
            collector = LogCollector(config.log_files, config.state_file)
            detector = Detector(config.min_status_events, cooldown_sec=300)
            self.assertIsNotNone(detector.detect(collector.collect()))
            # 같은 장애 로그가 또 쌓여도 cooldown 동안은 다시 알리지 않는다
            with open(config.log_files["app"], "a", encoding="utf-8") as f:
                f.write("\n".join(sc["app"]) + "\n")
            self.assertIsNone(detector.detect(collector.collect()))

    def test_collector_reads_only_new_lines(self):
        with tempfile.TemporaryDirectory() as d:
            config = make_env(Path(d), {"app": ["2026-11-25T10:00:00+0000 INFO [app] 시작"]})
            collector = LogCollector(config.log_files, config.state_file)
            self.assertEqual(len(collector.collect()), 1)
            self.assertEqual(len(collector.collect()), 0)

    def test_few_429_is_not_incident(self):
        with tempfile.TemporaryDirectory() as d:
            config = make_env(Path(d), {"nginx_access": [access(429)] * 2})
            events = LogCollector(config.log_files, config.state_file).collect()
            self.assertIsNone(Detector(3, 300).detect(events))

    def test_runbook_search(self):
        with tempfile.TemporaryDirectory() as d:
            tools = Toolbox(make_env(Path(d), {}))
            self.assertIn("DB 연결 실패", tools.search_runbook("DB 연결 실패 조치"))
            self.assertIn("429", tools.search_runbook("429 limiting requests"))


if __name__ == "__main__":
    unittest.main()
