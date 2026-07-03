# the-lab-zone-dockerfiles

Colecao de imagens Docker usadas no ecossistema The Lab Zone.

## Imagens

- `dbt-runner`
- `goose-runner`
- `lightrag-langfuse`
- `mcp-server-qdrant`
- `qa-indexer`

## Labels OCI

Todos os `Dockerfile` definem:

- `org.opencontainers.image.title`
- `org.opencontainers.image.authors`
- `org.opencontainers.image.vendor`
- `org.opencontainers.image.licenses`

O workflow em [`.github/workflows/publish.yaml`](.github/workflows/publish.yaml) adiciona no build:

- `org.opencontainers.image.source`
- `org.opencontainers.image.version`
- `org.opencontainers.image.revision`

## Publicacao

As imagens sao publicadas no GitHub Container Registry via GitHub Actions em pushes para a branch `main` quando ha alteracoes nas pastas das imagens.
