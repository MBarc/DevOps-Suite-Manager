FROM python:3.12-slim

# libgomp1: required by the fastembed ONNX runtime
# openssh-client: used by asyncssh for key negotiation helpers
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package. Copy everything first so hatchling can find the source.
COPY . .
RUN pip install --no-cache-dir .

ENV DOSM_HOME=/dosm-home
# Keep the fastembed model cache inside DOSM_HOME so it survives rebuilds.
ENV FASTEMBED_CACHE_PATH=/dosm-home/data/fastembed-cache

EXPOSE 8765

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
