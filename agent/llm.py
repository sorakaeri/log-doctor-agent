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
        """model은 쉼표로 여러 개 지정 가능 (앞이 기본, 뒤는 예비).
        예: "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
        """
        self.api_key = api_key
        self.models = [m.strip() for m in model.split(",") if m.strip()]
        self.timeout = timeout
        self.calls = 0          # 사용량 확인용 (무료 티어 한도 관리)
        self.last_model = self.models[0] if self.models else ""

    @property
    def model(self) -> str:
        return self.last_model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        contents: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        retries: int = 1,
    ) -> dict:
        """Gemini 응답의 첫 번째 candidate content({"role": "model", "parts": [...]})를 반환."""
        if not self.available:
            raise LLMError("GEMINI_API_KEY가 설정되지 않음")

        # Gemini 3 계열은 temperature 기본값(1.0) 유지를 권장하므로 따로 지정하지 않는다.
        body: dict = {"contents": contents, "generationConfig": {}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"

        payload = json.dumps(body).encode("utf-8")
        data, errors = None, []
        # 모델 목록을 앞에서부터 시도. 503(사용량 폭주)·429(한도 초과)면
        # 같은 모델을 다시 부르지 않고 바로 다음(예비) 모델로 넘어간다.
        # 모든 모델이 실패하면 잠시 쉬었다가 retries 횟수만큼 한 바퀴 더 시도한다.
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(5)
            for model in self.models:
                try:
                    data = self._post(model, payload)
                    self.last_model = model
                    break
                except LLMError as e:
                    errors.append(f"{model}: {e}")
                    if e.args and str(e).startswith("HTTP 400"):
                        raise  # 요청 형식 오류는 다른 모델로 바꿔도 같으므로 중단
                    print(f"  ! {model} 실패 → 다음 모델 시도 ({str(e)[:60]})")
            if data is not None:
                break
        if data is None:
            raise LLMError(" / ".join(errors)[-600:])

        try:
            return data["candidates"][0]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Gemini 응답 형식 오류: {json.dumps(data)[:300]}") from e

    def _post(self, model: str, payload: bytes) -> dict:
        req = urllib.request.Request(
            API_URL.format(model=model),
            data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise LLMError(f"HTTP {e.code}: {str(detail)[:250]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise LLMError(f"연결 실패: {e}") from e


def list_models(api_key: str) -> list[dict]:
    """이 API 키로 generateContent를 쓸 수 있는 모델 목록."""
    models, token = [], ""
    while True:
        url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"
        if token:
            url += f"&pageToken={token}"
        req = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models += [m for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        token = data.get("nextPageToken", "")
        if not token:
            return models


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
