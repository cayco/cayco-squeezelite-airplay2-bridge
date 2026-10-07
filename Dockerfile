# Stage 1: Build custom squeezelite binary with stdout silence-skip & unity gain patches
FROM debian:bookworm-slim AS builder

RUN sed -i "s/^Types: deb$/Types: deb deb-src/" /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        dpkg-dev && \
    apt-get build-dep -y squeezelite && \
    apt-get source squeezelite

WORKDIR /squeezelite-src
RUN cp -r /squeezelite-1.9.9*/* /squeezelite-src/

COPY patches/output_stdout.c /squeezelite-src/output_stdout.c
COPY patches/output_alsa.c /squeezelite-src/output_alsa.c

RUN make -j"$(nproc)" && strip squeezelite

# Stage 2: Runtime image with pyatv and patched squeezelite
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    squeezelite \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pyatv

WORKDIR /app
COPY --from=builder /squeezelite-src/squeezelite /usr/bin/squeezelite
COPY bridge.py /app/bridge.py

ENTRYPOINT ["python3", "-u", "/app/bridge.py"]
