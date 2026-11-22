# Clipper in a container (CPU encoding with libx264).
# For NVIDIA NVENC, build FROM an nvidia/cuda runtime image with an NVENC-enabled ffmpeg and run
# with `--gpus all`; the app auto-detects the encoder.
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CLIPPER_HOME=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates tini nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 clipper
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY clipper ./clipper
RUN pip install .

RUN mkdir -p /data && chown clipper:clipper /data
USER clipper
VOLUME ["/data"]
EXPOSE 8787

# tini forwards SIGTERM so ffmpeg segments are finalised on `docker stop`.
ENTRYPOINT ["/usr/bin/tini", "--", "clipper"]
CMD ["ui", "--host", "0.0.0.0"]
