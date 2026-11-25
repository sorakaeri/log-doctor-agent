# 트래픽 폭주 / 리소스 / 권한 장애 조치 문서

## 429 Too Many Requests (요청 폭주, rate limit 동작)
증상: Nginx access 로그에 429 다수, error 로그에 `limiting requests, excess: ... by zone "api"`.
의미: Nginx의 요청 제한(IP당 초당 10건, burst 20)이 동작해 초과 요청을 막고 있다. WAS는 보호되고 있는 상태.
확인:
- 요청이 특정 클라이언트 IP에 몰렸는지 확인 (공격/봇/잘못된 반복 호출 여부)
- `docker stats`로 WAS CPU/메모리가 한계인지 확인
주의:
- 172.18.0.1처럼 172.x.0.1로 끝나는 IP는 Docker 네트워크 게이트웨이다. PC(호스트)에서 들어오는 모든 요청이 이 IP로 보이므로 이 IP를 차단하면 서비스 전체가 막힌다. 절대 차단을 제안하지 않는다.
- 실제 사용자 IP를 구분하려면 앞단 로드밸런서가 넘겨주는 X-Forwarded-For 헤더를 로그에 남겨야 한다.
조치:
- 요청 출처가 실제 외부 IP이고 비정상 요청이라면 해당 IP 차단(deny) 또는 클라이언트 측 재시도 로직 확인
- 게이트웨이 IP라면 PC에서 실행 중인 스크립트·부하 테스트 등 요청을 보내는 프로그램을 먼저 확인
- 정상 사용자 트래픽 증가라면 `nginx.conf`의 `rate`/`burst` 조정과 WAS 확장 검토
- 조치 후 429 비율이 줄어드는지 access 로그로 확인

## 메모리 부족 (OOM)
증상: 컨테이너가 갑자기 종료, `docker inspect`에서 `OOMKilled=true`.
조치:
- docker-compose.yml의 `memory` 한도 상향 또는 메모리 누수 확인

## 권한 오류 (Permission denied)
증상: 로그에 `Permission denied`, `EACCES`.
조치:
- 오류 메시지의 파일 경로 확인
- 볼륨 마운트 경로의 소유자와 컨테이너 실행 사용자가 맞는지 확인
- Windows에서는 OneDrive 등 동기화 폴더에서 파일이 잠기는 경우가 있으므로 프로젝트를 일반 폴더(C:\dev 등)로 옮긴다
