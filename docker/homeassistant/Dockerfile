ARG HA_VERSION=stable
FROM ghcr.io/home-assistant/home-assistant:${HA_VERSION}

WORKDIR /tmp/tariffkit
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && rm -rf /tmp/tariffkit

WORKDIR /config
