# DB(PostgreSQL) 장애 조치 문서

## DB 연결 실패 (DB 컨테이너 중지)
증상: 앱 로그에 `DB 연결 실패`, Nginx access 로그에 503, `/health`는 200이지만 `/health/db`는 503.
확인:
- `docker compose ps db` 로 ld-db 상태가 exited인지 확인
- `docker logs ld-db --tail 50` 으로 종료 직전 로그 확인 (shutdown 요청인지, 오류로 종료됐는지)
조치:
- 수동 중지였다면 `docker compose start db` 후 `/health/db`가 200인지 확인
- 오류 종료였다면 로그의 원인(디스크 부족, 설정 오류)을 먼저 해결한 뒤 시작
- 재발 방지: 운영 환경에서는 `restart: unless-stopped`와 DB 헬스체크 알림을 설정

## DB 인증 실패 (password authentication failed)
증상: 앱 로그에 `password authentication failed for user`.
조치:
- `.env`의 `POSTGRES_PASSWORD`와 앱 환경 변수 `DB_PASSWORD`가 같은지 확인
- DB 볼륨이 이전 비밀번호로 초기화된 경우 비밀번호를 원래대로 맞추거나, 데이터 백업 후 볼륨을 다시 만든다

## DB 연결 수 초과 (too many connections)
증상: 앱 로그에 `too many clients already` 또는 `remaining connection slots are reserved`.
조치:
- 앱이 요청마다 연결을 새로 열고 닫지 않는지 확인하고, 연결 풀(pool) 도입을 검토
- 일시적이라면 트래픽 급증 여부를 함께 확인
