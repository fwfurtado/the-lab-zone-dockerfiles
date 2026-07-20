# Jobs do lakehouse: codigo como pacote instalado na imagem (substitui o ConfigMap
# de scripts). A tag desta imagem E a versao do codigo dos jobs.
# NAO adicionar jars aqui; classpath pertence a spark-delta.

FROM ghcr.io/fwfurtado/spark-delta:0.2.0

LABEL org.opencontainers.image.title="lakehouse-jobs" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

USER root

COPY pyproject.toml /opt/lakehouse-jobs/
COPY src /opt/lakehouse-jobs/src
RUN pip3 install --no-cache-dir /opt/lakehouse-jobs && rm -rf /opt/lakehouse-jobs

# entrypoints thin: spark-submit exige um ARQUIVO em mainApplicationFile;
# a logica real vive no pacote instalado
COPY entrypoints/ /opt/spark/app/

USER spark
