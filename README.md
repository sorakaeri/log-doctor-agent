# Log Doctor — 서버 장애 로그 분석 Agent

> 서버 로그를 AI가 분석해 **장애 원인과 조치 방법을 Slack으로 알려주는** Workflow + AI Agent 하이브리드 시스템

대구대학교 Workflow / AI Agent 개발 과제 (개인 프로젝트, 마감 2026-12-04)

## 1. 프로젝트 소개

서버에 장애가 나면 담당자는 여러 로그 파일을 열어 보고, 컨테이너 상태를 확인하고, 조치 문서를 찾아야 합니다. Log Doctor는 이 과정을 자동화합니다.

| 단계 | 방식 | 하는 일 |
|---|---|---|
| ① 로그 수집 | Workflow (코드) | 30초마다 Nginx·WAS 로그에서 새로 추가된 줄만 읽음 |
| ② 에러 감지·묶기 | Workflow (코드) | 에러만 골라 "에러 지문"으로 묶고, 이미 알린 장애는 5분간 다시 알리지 않음 |
| ③ 유형 분류 | **Routing** (LLM) | DB 연결 / 네트워크 / 리소스·트래픽 / 권한 중 하나로 분류 |
| ④ 원인 조사 | **AI Agent** (Tool Calling) | 유형별로 허용된 도구 중 필요한 것을 LLM이 스스로 골라 조사 |
| ⑤ 보고 | Workflow (코드) | 원인·근거·조치 방안을 Slack으로 전송 |

**설계 원칙**
- **읽기 전용 Agent**: 로그·상태 조회 도구만 제공합니다. 재시작·삭제 같은 변경은 하지 않고 조치를 "제안"만 합니다.
- **LLM이 실패해도 동작**: API 키가 없거나 무료 한도를 넘으면 키워드 규칙과 고정 절차로 대체해 알림은 계속 보냅니다.
- **알림 폭탄 방지**: 같은 장애가 반복되면 묶어서 한 번만 알립니다. LLM 호출도 장애 1건당 3~5회로 제한됩니다.

## 2. 시스템 처리 흐름도

```mermaid
flowchart TD
    subgraph Target["분석 대상 3-Tier (Docker Compose)"]
        N[Nginx :8080] --> A[FastAPI WAS] --> D[(PostgreSQL)]
    end
    N -. access.log / error.log .-> L[(logs/)]
    A -. app.log .-> L

    L --> C["① 로그 수집<br/>새로 추가된 줄만 (30초 주기)"]
    C --> DET{"② 에러 감지<br/>5xx·429·ERROR"}
    DET -- 없음 / 이미 알린 장애 --> C
    DET -- 신규 장애 --> R{"③ LLM 유형 분류<br/>(Routing)"}

    R -- DB_CONNECTION --> T1[DB 조사 도구 세트]
    R -- NETWORK --> T2[업스트림 조사 도구 세트]
    R -- RESOURCE --> T3[트래픽·리소스 도구 세트]
    R -- PERMISSION / UNKNOWN --> T4[기타 도구 세트]

    T1 & T2 & T3 & T4 --> AG

    subgraph AG["④ AI Agent 루프 (최대 6회)"]
        TH[LLM: 다음 행동 결정] -- 도구 호출 --> TL[도구 실행<br/>로그 검색 · 컨테이너 상태 · 리소스 ·<br/>엔드포인트 점검 · 조치 문서 검색]
        TL -- 결과 --> TH
    end

    AG -- 최종 보고 JSON --> S["⑤ Slack 알림<br/>원인 · 근거 · 조치 방안"]
    R -. LLM 실패 .-> FB[규칙 기반 분류·고정 절차] --> S
```

### Agent가 쓸 수 있는 도구 (모두 읽기 전용)

| 도구 | 하는 일 | 사용 유형 |
|---|---|---|
| `search_logs` | 로그 파일 키워드 검색 | 전체 |
| `get_container_status` | 컨테이너 상태, 종료 코드, OOM 여부 | 전체 |
| `get_container_logs` | `docker logs`로 종료 직전 로그 확인 | DB, 네트워크, 권한 |
| `check_endpoint` | `/health`, `/health/db` 등 현재 응답 확인 | DB, 네트워크 |
| `get_resource_usage` | CPU·메모리 사용량 (`docker stats`) | 리소스 |
| `summarize_traffic` | 상태 코드 분포, 요청 많은 IP·URI | 리소스 |
| `search_runbook` | 조치 문서(`agent/runbooks/`) 검색 (간단한 RAG) | 전체 |

## 3. 사용 기술

| 영역 | 기술 | 비고 |
|---|---|---|
| 분석 대상 인프라 | Docker Compose, Nginx 1.27, FastAPI, PostgreSQL 16 | 무료, 로컬에서 장애 재현 |
| Agent | Python 3.11+ **표준 라이브러리만 사용** | `pip install` 없이 실행 |
| LLM | Google Gemini API (REST, Function Calling) | 무료 티어 Flash Lite(하루 500회), 503·429 시 예비 모델로 자동 전환 |
| 알림 | Slack Incoming Webhook (Block Kit 메시지) | 무료 |
| 적용한 개념 | Prompt Chaining, Routing, Tool Calling(Agent 루프), RAG(키워드 검색), Fallback | |

Agent 루프는 LangChain·LangGraph 같은 프레임워크 없이 직접 구현했습니다. "LLM 응답에 함수 호출이 있으면 도구를 실행하고 결과를 다시 넘기며, 없으면 최종 답으로 본다"는 Agent의 핵심 구조가 `agent/investigator.py`에 그대로 드러나도록 하기 위해서입니다.

## 4. 실행 방법

### 사전 준비
- Docker Desktop (Windows는 WSL2 기반 권장)
- Python 3.11 이상
- (선택) Gemini API 키: https://aistudio.google.com/apikey
- (선택) Slack Incoming Webhook URL: https://api.slack.com/apps → 앱 생성 → Incoming Webhooks 켜기 → 채널 선택

### 1) 3-Tier 환경 실행

```bash
git clone https://github.com/sorakaeri/log-doctor-agent.git
cd log-doctor-agent
cp .env.example .env              # Windows PowerShell: Copy-Item .env.example .env
# .env에 GEMINI_API_KEY, SLACK_WEBHOOK_URL 입력

docker compose up -d --build
python scripts/chaos.py status    # 세 경로 모두 200이면 정상
```

### 2) Agent 점검

```bash
python scripts/list_models.py     # 내 API 키로 쓸 수 있는 Gemini 모델 ID 확인
python -m agent --check           # 로그 파일, Gemini 연결 확인
python -m agent --test-slack      # Slack에 테스트 메시지 전송
```

### 3) Agent 실행 + 장애 발생

터미널 두 개를 엽니다.

```bash
# 터미널 1: Agent 감시 시작 (30초마다 확인)
python -m agent

# 터미널 2: 장애 발생 → 30초 안에 Slack 알림 확인 → 복구
python scripts/chaos.py db-down
python scripts/chaos.py recover
```

| 명령 | 시나리오 | 기대 분류 |
|---|---|---|
| `python scripts/chaos.py db-down` | DB 컨테이너 중지 → 앱 503 | DB 연결 장애 |
| `python scripts/chaos.py app-down` | WAS 컨테이너 중지 → Nginx 502 | 네트워크/업스트림 연결 장애 |
| `python scripts/chaos.py flood` | 요청 폭주 → Nginx 429 | 리소스 부족/트래픽 폭주 |
| `python scripts/chaos.py recover` | 전체 복구 | |

시나리오 사이에는 `recover` 후 다음 시나리오를 실행합니다. 같은 장애는 5분(`AGENT_COOLDOWN_SEC`) 동안 다시 알리지 않습니다.

### 기타 옵션

```bash
python -m agent --once --dry-run      # 한 번만 확인, Slack 대신 콘솔에 출력
python -m agent --once --from-start   # 기존 로그 전체를 한 번 분석
python -m unittest -v                 # 테스트 (Docker·Gemini·Slack 없이 실행)
```

## 5. 디렉터리 구조

```
log-doctor-agent/
├── docker-compose.yml      # 분석 대상 3-Tier
├── nginx/nginx.conf        # JSON access 로그, 요청 제한(429)
├── app/                    # FastAPI WAS (상품 API)
├── db/init.sql             # 초기 데이터
├── scripts/chaos.py        # 장애 시나리오 실행
├── agent/
│   ├── main.py             # 실행 진입점, 전체 흐름 ①~⑤
│   ├── collector.py        # ① 로그 수집 (새 줄만 읽기)
│   ├── detector.py         # ② 에러 감지, 지문으로 묶기, 중복 알림 방지
│   ├── classifier.py       # ③ LLM 유형 분류 (Routing) + 규칙 기반 대체
│   ├── tools.py            # Agent 도구 (읽기 전용) + 유형별 도구 세트
│   ├── investigator.py     # ④ Agent 루프 (Function Calling)
│   ├── reporter.py         # ⑤ Slack 메시지 생성·전송
│   ├── llm.py              # Gemini REST 클라이언트
│   ├── config.py           # .env 설정
│   └── runbooks/           # 조치 문서 (RAG 검색 대상)
├── tests/                  # 가짜 LLM으로 전체 흐름 테스트
├── logs/                   # 각 계층 로그 (Git 제외)
└── docs/                   # 실행 결과 스크린샷
```

## 6. 진행 현황

| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | 3-Tier 환경, 장애 시나리오 스크립트 | ✅ 로컬 실행 확인 (11/25) |
| 2 | 로그 수집·에러 감지, LLM 분류, Slack 전송 | ✅ 코드·테스트 완료 |
| 3 | Agent 도구, 조치 문서 RAG, 중복 알림 묶기 | ✅ 코드·테스트 완료 |
| 4 | 실제 환경에서 시나리오 3개 실행, 스크린샷·시연 영상 | ⬜ |

## 7. 실행 결과

(시나리오별 Slack 알림 스크린샷과 시연 영상 추가 예정)

## 8. 참고 자료 및 AI 도구 활용

- 코드 초안 작성과 설계 검토에 Anthropic Claude를 활용했습니다. 설계 결정(읽기 전용, Routing 기준, 중복 알림 방지, 대상 시나리오)은 직접 정하고 검토했습니다.
- 참고한 유사 프로젝트
  - [HolmesGPT](https://github.com/HolmesGPT/holmesgpt): 오픈소스 AI SRE Agent (가설 → 도구로 검증하는 조사 구조)
  - [K8sGPT](https://github.com/k8sgpt-ai/k8sgpt): 규칙 기반 탐지 + LLM 설명 구조 (본 프로젝트의 Fallback 설계에 참고)
  - [AI Log Analysis with Ollama](https://www.virtua.cloud/learn/en/tutorials/ai-log-analysis-ollama-vps): 로그 → LLM → 알림 파이프라인
  - [kprompt: Stop paging on every event](https://dev.to/muhtalipdede/stop-paging-on-every-kubelet-event-incidents-confidence-gates-45nk): 이벤트를 장애 단위로 묶어 알림 폭탄을 막는 아이디어
- [Gemini API Function Calling 문서](https://ai.google.dev/gemini-api/docs/function-calling), [Slack Incoming Webhooks 문서](https://api.slack.com/messaging/webhooks)

## 보안

API 키, Slack Webhook URL, DB 비밀번호는 `.env`에만 저장하며 `.gitignore`로 Git에서 제외합니다.
