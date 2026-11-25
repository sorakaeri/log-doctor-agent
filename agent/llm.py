"""Gemini API 클라이언트 (REST 직접 호출, 표준 라이브러리만 사용).

- generate(): 일반 텍스트/JSON 응답
- Function Calling: tools에 함수 선언을 넘기면 모델이 functionCall을 돌려준다.
"""

import json
import time
import urllib.error
import urllib.request

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    pass


class GeminiClient:
    def __init__(self, api_key: str, model: str, timeout: int = 60):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.calls = 0          # 사용량 확인용 (무료 티어 한도 관리)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        contents: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        retries: int = 2,
    ) -> dict:
        """Gemini 응답의 첫 번째 candidate content({"role": "model", "parts": [...]})를 반환."""
        if not self.available:
            raise LLMError("GEMINI_API_KEY가 설정되지 않음")

        body: dict = {"contents": contents, "generationConfig": {"temperature": 0.2}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"

        req = urllib.request.Request(
            API_URL.format(model=self.model),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        for attempt in range(retries + 1):
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:300]
                # 429(무료 한도 초과)나 5xx는 잠시 기다렸다가 재시도
                if e.code in (429, 500, 503) and attempt < retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise LLMError(f"Gemini HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries:
                    time.sleep(3)
                    continue
                raise LLMError(f"Gemini 연결 실패: {e}") from e

        try:
            return data["candidates"][0]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Gemini 응답 형식 오류: {json.dumps(data)[:300]}") from e


def text_of(content: dict) -> str:
    return "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought"))


def function_calls(content: dict) -> list[dict]:
    return [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]


def parse_json(text: str) -> dict:
    """모델이 ```json ... ``` 으로 감싸서 답해도 JSON을 꺼낸다."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"JSON을 찾을 수 없음: {text[:200]}")
    return json.loads(text[start : end + 1])
