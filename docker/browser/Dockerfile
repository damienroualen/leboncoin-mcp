FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl fonts-liberation fonts-noto-color-emoji \
      novnc openbox socat websockify x11vnc xvfb \
 && curl -fsSL -o /tmp/chrome.deb \
      https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*

RUN useradd --create-home chrome \
 && mkdir -p /data/chrome \
 && chown -R chrome:chrome /data

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER chrome
VOLUME /data/chrome
EXPOSE 6080 9222
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
