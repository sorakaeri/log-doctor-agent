# WAS(FastAPI) / Nginx 업스트림 장애 조치 문서

## 502 Bad Gateway (WAS 컨테이너 중지)
증상: Nginx access 로그에 502, Nginx error 로그에 `app could not be resolved` 또는 `connect() failed (111: Connection refused) while connecting to upstream`.
확인:
- `docker compose ps app` 로 ld-app 상태 확인 (exited이면 WAS 중지)
- `docker inspect ld-app --format "{{.State.ExitCode}} {{.State.OOMKilled}}"` 로 종료 코드 확인
  - 종료 코드 0 또는 137 + OOMKilled=false: 사람이 중지했을 가능성 (docker stop)
  - OOMKilled=true: 메모리 한도(256M) 초과로 강제 종료
  - 1: 애플리케이션 오류로 종료 → `docker logs ld-app --tail 50`에서 traceback 확인
조치:
- 수동 중지였다면 `docker compose start app` 후 `/health`가 200인지 확인
- OOM이었다면 메모리 한도 상향 또는 메모리 사용 원인 분석
- 앱 오류였다면 traceback의 원인을 수정 후 `docker compose up -d --build app`

## 504 Gateway Timeout (WAS 응답 지연)
증상: Nginx error 로그에 `upstream timed out`.
조치:
- `docker stats`로 ld-app CPU 사용률 확인
- 느린 API와 DB 쿼리 확인, 필요 시 `proxy_read_timeout` 조정
