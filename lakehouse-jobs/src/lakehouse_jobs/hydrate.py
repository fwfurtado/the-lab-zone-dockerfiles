"""Hidratação one-shot do Pinot (etapa 2.3).

Problema: o streaming CDF começou do latest -- contas nunca tocadas depois disso
não existem no tópico, e a tabela upsert do Pinot (realtime-only, sem perna
batch) nasce com um buraco exatamente nas contas frias.

Solução: ler o snapshot da Delta na versão V e produzir TODAS as linhas vivas no
MESMO tópico, mesmo formato do cdf_to_kafka, com _commit_version = V. O upsert
do Pinot compara por _commit_version, então:
  - conta fria (sem msg no tópico): a da hidratação vence por W.O.
  - conta mudada após V: a msg viva do CDF tem _commit_version > V e vence,
    independente da ordem de chegada
=> ZERO coordenação: gerador e streaming seguem rodando durante a hidratação.
Idempotente: rerun lê um V novo e reproduz o estado -- inócuo.

_change_type = "hydrate" de propósito: distingue origem nas validações de
paridade ("quantas linhas do Pinot vieram da hidratação?").
"""

import os

from pyspark.sql import SparkSession
from pyspark.sql import functions as F


def main():
    table_path = os.environ.get("TABLE_PATH", "s3a://lakehouse/tables/accounts")
    topic = os.environ.get("KAFKA_TOPIC", "accounts.cdf")
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "redpanda.data.svc.cluster.local:9093")
    username = os.environ["KAFKA_USERNAME"]
    password = os.environ["KAFKA_PASSWORD"]
    ca_path = os.environ.get("KAFKA_CA_PATH", "/etc/redpanda-ca/ca.crt")

    spark = SparkSession.builder.appName("hydrate-accounts").getOrCreate()

    # snapshot pinado: versão + timestamp do commit V (o timestamp vira o
    # _commit_timestamp das mensagens -- semanticamente "o estado em V")
    head = spark.sql(f"DESCRIBE HISTORY delta.`{table_path}` LIMIT 1").collect()[0]
    version, version_ts = head["version"], head["timestamp"]

    snapshot = (
        spark.read.format("delta")
        .option("versionAsOf", version)
        .load(table_path)
    )

    # payload espelha o cdf_to_kafka (mesmas colunas, mesmo to_json) -- o Pinot
    # não distingue transporte, só conteúdo
    payload = snapshot.select(
        F.col("account_id").cast("string").alias("key"),
        F.to_json(
            F.struct(
                "account_id", "status", "tier", "region", "balance", "version",
                "updated_at",
                F.lit(False).alias("deleted"),
                F.lit("hydrate").alias("_change_type"),
                F.lit(version).alias("_commit_version"),
                F.lit(version_ts).cast("timestamp").alias("_commit_timestamp"),
            )
        ).alias("value"),
    )

    jaas = (
        "org.apache.kafka.common.security.scram.ScramLoginModule required "
        f'username="{username}" password="{password}";'
    )

    total = payload.count()
    print(f"HYDRATE_START version={version} rows={total}")

    (
        payload.write.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("kafka.security.protocol", "SASL_SSL")
        .option("kafka.sasl.mechanism", "SCRAM-SHA-512")
        .option("kafka.sasl.jaas.config", jaas)
        .option("kafka.ssl.truststore.type", "PEM")
        .option("kafka.ssl.truststore.location", ca_path)
        .option("topic", topic)
        .save()
    )

    print(f"HYDRATE_OK version={version} rows={total}")
    spark.stop()
