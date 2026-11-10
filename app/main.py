"""Log Doctor 대상 WAS: 간단한 상품(items) API.

장애 분석 Agent가 읽을 수 있도록 모든 요청과 오류를 로그 파일에 남긴다.
DB 연결이 끊기면 503을 반환하고 원인(예외 메시지)을 ERROR 로그로 기록한다.
"""

import logging
import os
import time

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 로깅: 파일 + 표준출력. 한 줄 형식 "시간 레벨 [app] 메시지"
# ---------------------------------------------------------------------------
LOG_FILE = os.getenv("LOG_FILE", "logs/app.log")
os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

formatter = logging.Formatter(
    "%(asctime)s %(levelname)s [app] %(message)s", "%Y-%m-%dT%H:%M:%S%z"
)
logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
for handler in (logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()):
    handler.setFormatter(formatter)
    logger.addHandler(handler)

DB_DSN = (
    f"host={os.getenv('DB_HOST', 'localhost')} "
    f"port={os.getenv('DB_PORT', '5432')} "
    f"dbname={os.getenv('DB_NAME', 'shop')} "
    f"user={os.getenv('DB_USER', 'shop')} "
    f"password={os.getenv('DB_PASSWORD', '')} "
    "connect_timeout=3"
)

app = FastAPI(title="Log Doctor Target App")


def get_conn() -> psycopg.Connection:
    """요청마다 새 연결을 연다. (단순함 우선, 1주차 범위)"""
    return psycopg.connect(DB_DSN)


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price: int = Field(ge=0)


# ---------------------------------------------------------------------------
# 미들웨어: 모든 요청을 로그로 남긴다
# ---------------------------------------------------------------------------
@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    level = logging.WARNING if response.status_code >= 500 else logging.INFO
    logger.log(
        level,
        "%s %s -> %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.exception_handler(psycopg.OperationalError)
async def db_error_handler(request: Request, exc: psycopg.OperationalError):
    # 예외 메시지에 "connection refused", "could not translate host name" 등
    # 원인 단서가 들어 있으므로 그대로 기록한다.
    message = str(exc).strip().replace("\n", " ")
    logger.error("DB 연결 실패: %s", message)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    logger.info("애플리케이션 시작 (pid=%d)", os.getpid())


@app.get("/health")
def health():
    """WAS 자체 상태만 확인 (DB와 무관)."""
    return {"status": "ok"}


@app.get("/health/db")
def health_db():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "db": "connected"}


@app.get("/api/items")
def list_items():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, price FROM items ORDER BY id"
        ).fetchall()
    return [{"id": r[0], "name": r[1], "price": r[2]} for r in rows]


@app.get("/api/items/{item_id}")
def get_item(item_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, price FROM items WHERE id = %s", (item_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {"id": row[0], "name": row[1], "price": row[2]}


@app.post("/api/items", status_code=201)
def create_item(item: ItemIn):
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO items (name, price) VALUES (%s, %s) RETURNING id",
            (item.name, item.price),
        ).fetchone()
    logger.info("상품 등록: id=%d name=%s", row[0], item.name)
    return {"id": row[0], "name": item.name, "price": item.price}
