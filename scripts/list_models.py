"""내 Gemini API 키로 사용할 수 있는 텍스트 모델 ID 목록을 출력한다.

사용법 (프로젝트 루트에서):  python scripts/list_models.py
출력된 ID를 .env의 GEMINI_MODEL에 쉼표로 이어서 적으면 된다. (앞이 기본, 뒤가 예비)
"""

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Config  # noqa: E402
from agent.llm import list_models  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

config = Config.load()
if not config.gemini_api_key:
    sys.exit(".env에 GEMINI_API_KEY가 없습니다.")

try:
    models = list_models(config.gemini_api_key)
except urllib.error.HTTPError as e:
    sys.exit(f"조회 실패: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")

print(f"현재 설정 GEMINI_MODEL={config.gemini_model}\n")
print(f"{'모델 ID':<40} 표시 이름")
for m in sorted(models, key=lambda m: m["name"]):
    model_id = m["name"].removeprefix("models/")
    mark = " ◀ 사용 중" if model_id in config.gemini_model.split(",") else ""
    print(f"{model_id:<40} {m.get('displayName', '')}{mark}")
