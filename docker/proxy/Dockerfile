# 에이전트 컨테이너의 유일한 외부 출구 (기획안 9장, Phase 3b).
# 내부 전용 네트워크의 컨테이너는 이 프록시를 거쳐 filter에 적힌 도메인에만 HTTPS로 나갈 수 있다.
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter /etc/tinyproxy/filter

EXPOSE 3128
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
