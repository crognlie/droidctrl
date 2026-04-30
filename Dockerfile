FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    adb \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    xclip \
    python3 \
    python3-flask \
    # scrcpy runtime deps
    ffmpeg \
    libsdl2-2.0-0 \
    libv4l-dev \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Pin scrcpy version for reproducible builds (known-good with Android 16 on Pixel 6a)
ARG SCRCPY_VERSION=v3.3.4
RUN set -ex && \
    curl -fL "https://github.com/Genymobile/scrcpy/releases/download/${SCRCPY_VERSION}/scrcpy-linux-x86_64-${SCRCPY_VERSION}.tar.gz" \
        | tar -xz -C /tmp && \
    DIR=$(ls -d /tmp/scrcpy-linux-x86_64-*) && \
    install -m 755 "$DIR/scrcpy" /usr/local/bin/scrcpy && \
    install -m 644 "$DIR/scrcpy-server" /usr/local/bin/scrcpy-server && \
    rm -rf /tmp/scrcpy-* && \
    ln -s /usr/bin/adb /usr/local/bin/adb

ARG GNIREHTET_VERSION=v2.5.1
RUN set -ex && \
    curl -fL "https://github.com/Genymobile/gnirehtet/releases/download/${GNIREHTET_VERSION}/gnirehtet-rust-linux64-${GNIREHTET_VERSION}.zip" \
        -o /tmp/gnirehtet.zip && \
    unzip /tmp/gnirehtet.zip -d /tmp/gnirehtet && \
    install -m 755 /tmp/gnirehtet/gnirehtet-rust-linux64/gnirehtet /usr/local/bin/gnirehtet && \
    install -m 644 /tmp/gnirehtet/gnirehtet-rust-linux64/gnirehtet.apk /usr/local/bin/gnirehtet.apk && \
    rm -rf /tmp/gnirehtet*

COPY start.sh /start.sh
COPY clipboard.py /clipboard.py
COPY usb_reset.py /usb_reset.py
RUN chmod +x /start.sh /usb_reset.py

EXPOSE 6080
EXPOSE 6081

CMD ["/start.sh"]
