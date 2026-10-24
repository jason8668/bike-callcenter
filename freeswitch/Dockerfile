# FreeSWITCH for the bike call center (internal test first, 400 trunk later).
# Debian's freeswitch packages, all mod packages included so no single
# missing module can break the build. Config is NOT baked in: the
# install script generates conf-live/ from conf-templates/ on the server
# (extension passwords + bridge secret must never be in Git) and mounts
# it at /etc/freeswitch. Host networking so SIP/RTP need no NAT tricks
# and 127.0.0.1:8890 reaches the bridge's published port.
FROM debian:13-slim

# Domestic mirror: direct foreign access from this host is unreliable.
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    apt-get update && \
    apt-get install -y --no-install-recommends curl procps \
      $(apt-cache search --names-only '^freeswitch$' | cut -d' ' -f1) \
      $(apt-cache search --names-only '^freeswitch-mod-' | cut -d' ' -f1) && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    ls /usr/lib/freeswitch/mod/ | sort

RUN mkdir -p /var/lib/freeswitch/db /var/log/freeswitch /var/run/freeswitch /recordings

COPY *.lua /usr/share/freeswitch/scripts/

# Documentation only under host networking; the real rules are the
# security-group rules (UDP 5060 + UDP 16384-16884).
EXPOSE 5060/udp 5060/tcp 8021/tcp

CMD ["freeswitch", "-nonat", "-nc"]
