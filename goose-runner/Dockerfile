FROM debian:bookworm-slim

LABEL org.opencontainers.image.title="goose-runner" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

ARG GOOSE_VERSION=v3.24.1

RUN apt-get update -q \
 && apt-get install -y --no-install-recommends ca-certificates curl jq bash \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://github.com/pressly/goose/releases/download/${GOOSE_VERSION}/goose_linux_x86_64" \
      -o /usr/local/bin/goose \
 && chmod +x /usr/local/bin/goose

ENTRYPOINT ["goose"]
