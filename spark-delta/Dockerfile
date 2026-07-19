# Imagem base para todos os jobs Spark do lakehouse (gerador, streaming CDF, manutenção).
#
# Matriz de versões (verificada nos pom.xml upstream — não alterar isoladamente):
#   Spark 4.1.2  -> Hadoop 3.4.2 (pom do Spark) -> hadoop-aws 3.4.2 -> AWS SDK v2 bundle 2.29.52 (pom do Hadoop 3.4.2)
#   Delta 4.2.0  -> construído sobre Spark 4.1.x / Scala 2.13 (artefato com sufixo: delta-spark_4.1_2.13)
#   kafka-clients 3.9.1 + commons-pool2 2.12.1 -> versões esperadas pelo spark-sql-kafka do Spark 4.1.2
#
# Nota Garage: o AWS SDK v2 >= 2.30.0 introduziu checksums de integridade por padrão que quebram
# S3 não-AWS (mesma classe de problema do Barman). O bundle 2.29.52 (pinado pelo Hadoop 3.4.2)
# é ANTERIOR a essa mudança — não fazer bump do bundle sem revalidar contra o Garage.

FROM spark:4.1.2-scala2.13-java17-python3-ubuntu

LABEL org.opencontainers.image.title="spark-delta" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

USER root

ENV DELTA_VERSION=4.2.0 \
    DELTA_SPARK_SUFFIX=4.1_2.13 \
    HADOOP_AWS_VERSION=3.4.2 \
    AWS_SDK_V2_VERSION=2.29.52 \
    SPARK_VERSION=4.1.2 \
    KAFKA_CLIENTS_VERSION=3.9.1 \
    COMMONS_POOL2_VERSION=2.12.1

# Jars direto em $SPARK_HOME/jars (classpath default): sem spark.jars.packages,
# sem download em runtime, sem egress pro Maven a partir do cluster.
# Cada jar é verificado contra o .sha1 publicado pelo próprio Maven Central.
RUN set -eux; \
    cd "${SPARK_HOME}/jars"; \
    MAVEN="https://repo1.maven.org/maven2"; \
    fetch() { \
        url="$1"; f="${url##*/}"; \
        curl -fSL --retry 3 -o "$f" "$url"; \
        curl -fSL --retry 3 -o "$f.sha1" "$url.sha1"; \
        printf '%s  %s\n' "$(awk '{print $1}' "$f.sha1")" "$f" | sha1sum -c -; \
        rm -f "$f.sha1"; \
    }; \
    fetch "${MAVEN}/io/delta/delta-spark_${DELTA_SPARK_SUFFIX}/${DELTA_VERSION}/delta-spark_${DELTA_SPARK_SUFFIX}-${DELTA_VERSION}.jar"; \
    fetch "${MAVEN}/io/delta/delta-storage/${DELTA_VERSION}/delta-storage-${DELTA_VERSION}.jar"; \
    fetch "${MAVEN}/org/apache/hadoop/hadoop-aws/${HADOOP_AWS_VERSION}/hadoop-aws-${HADOOP_AWS_VERSION}.jar"; \
    fetch "${MAVEN}/software/amazon/awssdk/bundle/${AWS_SDK_V2_VERSION}/bundle-${AWS_SDK_V2_VERSION}.jar"; \
    fetch "${MAVEN}/org/apache/spark/spark-sql-kafka-0-10_2.13/${SPARK_VERSION}/spark-sql-kafka-0-10_2.13-${SPARK_VERSION}.jar"; \
    fetch "${MAVEN}/org/apache/spark/spark-token-provider-kafka-0-10_2.13/${SPARK_VERSION}/spark-token-provider-kafka-0-10_2.13-${SPARK_VERSION}.jar"; \
    fetch "${MAVEN}/org/apache/kafka/kafka-clients/${KAFKA_CLIENTS_VERSION}/kafka-clients-${KAFKA_CLIENTS_VERSION}.jar"; \
    fetch "${MAVEN}/org/apache/commons/commons-pool2/${COMMONS_POOL2_VERSION}/commons-pool2-${COMMONS_POOL2_VERSION}.jar"; \
    chown spark:spark ./*.jar

# numpy para a amostragem Zipfian do gerador
RUN pip3 install --no-cache-dir numpy==2.5.1

USER spark