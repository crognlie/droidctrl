FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    adb \
    python3 \
    python3-aiohttp \
    usbutils \
    unzip \
    && rm -rf /var/lib/apt/lists/*

ARG GNIREHTET_VERSION=v2.5.1
RUN set -ex && \
    curl -fL "https://github.com/Genymobile/gnirehtet/releases/download/${GNIREHTET_VERSION}/gnirehtet-rust-linux64-${GNIREHTET_VERSION}.zip" \
        -o /tmp/gnirehtet.zip && \
    unzip /tmp/gnirehtet.zip -d /tmp/gnirehtet && \
    install -m 755 /tmp/gnirehtet/gnirehtet-rust-linux64/gnirehtet /usr/local/bin/gnirehtet && \
    install -m 644 /tmp/gnirehtet/gnirehtet-rust-linux64/gnirehtet.apk /usr/local/bin/gnirehtet.apk && \
    rm -rf /tmp/gnirehtet*

COPY start.sh /start.sh
COPY usb_reset.py /usb_reset.py
COPY stream_server.py /stream_server.py
COPY web/player.html /webcodecs/index.html
RUN chmod +x /start.sh /usb_reset.py

EXPOSE 6080
EXPOSE 6081

CMD ["/start.sh"]
