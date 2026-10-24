# FreeSWITCH for the bike call center (internal test first, 400 trunk later).
#
# Why NOT plain Debian: Debian dropped the whole FreeSWITCH stack
# (packages.debian.org search for "freeswitch" is empty in every suite,
# including trixie and bookworm). The first version of this file used
# Debian's repo with a dynamic package search, which silently installed
# nothing and produced an image without the freeswitch binary -- the
# container then failed with 'executable file not found in $PATH'.
# This version therefore uses SignalWire's official repo (bookworm)
# with EXPLICIT package names (apt fails loudly on unknown names) and
# a hard post-install check: if the binary or any required module is
# missing, the build FAILS here instead of shipping a dead image.
#
# The SignalWire repo needs a free Personal Access Token (since 1.10.7).
# The token is supplied as a BuildKit secret, never as a build arg and
# never stored in any image layer: the install script builds with
#   docker buildx build --secret id=sw_token,src=../freeswitch/signalwire-token.txt
# and the apt auth file is removed in the same RUN step that used it.
# Config is NOT baked in: the install script generates conf-live/ from
# conf-templates/ on the server (extension passwords + bridge secret
# must never be in Git) and mounts it at /etc/freeswitch. Host
# networking so SIP/RTP need no NAT tricks and 127.0.0.1:8890 reaches
# the bridge's published port.
FROM debian:12-slim

# Domestic mirror for Debian packages (bookworm uses classic sources.list).
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg2 wget ca-certificates procps && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN --mount=type=secret,id=sw_token \
    TOKEN=$(cat /run/secrets/sw_token | tr -d '\r\n') && \
    test -n "$TOKEN" && \
    wget --http-user=signalwire --http-password="$TOKEN" \
      -O /usr/share/keyrings/signalwire-freeswitch-repo.gpg \
      https://freeswitch.signalwire.com/repo/deb/debian-release/signalwire-freeswitch-repo.gpg && \
    echo "machine freeswitch.signalwire.com login signalwire password $TOKEN" \
      > /etc/apt/auth.conf.d/freeswitch.conf && \
    chmod 600 /etc/apt/auth.conf.d/freeswitch.conf && \
    echo "deb [signed-by=/usr/share/keyrings/signalwire-freeswitch-repo.gpg] https://freeswitch.signalwire.com/repo/deb/debian-release/ bookworm main" \
      > /etc/apt/sources.list.d/freeswitch.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      freeswitch \
      freeswitch-mod-sofia freeswitch-mod-callcenter freeswitch-mod-json-cdr \
      freeswitch-mod-lua freeswitch-mod-curl freeswitch-mod-dptools \
      freeswitch-mod-dialplan-xml freeswitch-mod-event-socket freeswitch-mod-commands \
      freeswitch-mod-console freeswitch-mod-logfile freeswitch-mod-loopback \
      freeswitch-mod-native-file freeswitch-mod-sndfile freeswitch-mod-tone-stream && \
    rm -f /etc/apt/auth.conf.d/freeswitch.conf && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    command -v freeswitch && command -v fs_cli && \
    for m in mod_sofia mod_callcenter mod_json_cdr mod_lua mod_curl mod_dptools mod_dialplan_xml mod_event_socket mod_commands mod_console mod_logfile mod_loopback mod_native_file mod_sndfile mod_tone_stream; do \
      test -f /usr/lib/freeswitch/mod/$m.so || { echo "MISSING MODULE $m"; exit 1; }; \
    done && \
    ls /usr/lib/freeswitch/mod/

RUN mkdir -p /var/lib/freeswitch/db /var/log/freeswitch /var/run/freeswitch /recordings

COPY *.lua /usr/share/freeswitch/scripts/

# Documentation only under host networking; the real rules are the
# security-group rules (UDP 5060 + UDP 16384-16884).
EXPOSE 5060/udp 5060/tcp 8021/tcp

CMD ["freeswitch", "-nonat", "-nc"]
