# just docker build-and-push --app-name goose-runner [--tag "<TAG>" | default "latest" ]   # -> pinar em chart/values.yaml
#
# Tag não é contrato; o digest é.
FROM debian:bookworm-slim

ARG GOOSE_VERSION=v3.24.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl jq bash \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://github.com/pressly/goose/releases/download/${GOOSE_VERSION}/goose_linux_x86_64" \
      -o /usr/local/bin/goose \
 && chmod +x /usr/local/bin/goose

ENTRYPOINT ["goose"]
