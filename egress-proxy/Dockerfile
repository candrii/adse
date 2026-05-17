FROM alpine:3.20

RUN apk add --no-cache tinyproxy wget && \
    addgroup -S proxy && adduser -S -G proxy proxy

COPY tinyproxy.conf  /etc/tinyproxy/tinyproxy.conf
COPY allowlist.txt   /etc/tinyproxy/filter
COPY healthz.sh      /usr/local/bin/healthz.sh

RUN chmod +x /usr/local/bin/healthz.sh

EXPOSE 8888
USER proxy
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
