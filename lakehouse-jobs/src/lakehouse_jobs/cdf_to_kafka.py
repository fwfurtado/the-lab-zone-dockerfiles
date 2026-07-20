"""Streaming CDF -> Redpanda (etapa 1.5).

Le o Change Data Feed da tabela accounts incrementalmente e publica no topico
Kafka `accounts.cdf`. Semantica fim-a-fim: at-least-once (sink Kafka do Spark nao
e transacional; batch reprocessado apos crash duplica mensagens IDENTICAS) --
inocuo para o Pinot, cujo upsert por PK e idempotente.

Decisoes:
- SEM startingVersion: a primeira execucao emite o snapshot atual da tabela como
  `insert` e depois segue incremental -- hidrata o Pinot sem backfill separado.
- filtro de update_preimage: o consumidor so precisa do estado novo.
- delete vira mensagem completa com deleted=true (deleteRecordColumn do Pinot),
  NAO tombstone null (que e para topicos compactados).
- key = account_id: ordem por chave preservada (mesma particao) -> upsert correto.
- Checkpoint no Garage: offsets = (versao do commit Delta, indice). O resume exige
  que as versoes pendentes ainda existam -- dai deletedFileRetentionDuration=6h
  na tabela; ficar parado alem disso quebra o resume com FileNotFound.
"""

import os

from pyspark.sql import SparkSession
from pyspark.sql import functions as F


def main():
    TABLE_PATH = os.environ.get("TABLE_PATH", "s3a://lakehouse/tables/accounts")
    TOPIC = os.environ.get("KAFKA_TOPIC", "accounts.cdf")
    BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "redpanda.data.svc.cluster.local:9093")
    CHECKPOINT = os.environ.get("CHECKPOINT_PATH", "s3a://lakehouse/_checkpoints/accounts-cdf")
    TRIGGER_SECONDS = int(os.environ.get("TRIGGER_SECONDS", "30"))
    KAFKA_USERNAME = os.environ["KAFKA_USERNAME"]
    KAFKA_PASSWORD = os.environ["KAFKA_PASSWORD"]
    KAFKA_CA_PATH = os.environ.get("KAFKA_CA_PATH", "/etc/redpanda-ca/ca.crt")

    spark = SparkSession.builder.appName("cdf-to-kafka").getOrCreate()

    # jaas montado em codigo a partir do Secret (nunca em sparkConf, que vaza no CRD)
    jaas = (
        "org.apache.kafka.common.security.scram.ScramLoginModule required "
        f'username="{KAFKA_USERNAME}" password="{KAFKA_PASSWORD}";'
    )

    changes = (
        spark.readStream.format("delta")
        .option("readChangeFeed", "true")
        .load(TABLE_PATH)
        .filter(F.col("_change_type") != "update_preimage")
        .withColumn("deleted", F.col("_change_type") == F.lit("delete"))
    )

    payload = changes.select(
        F.col("account_id").cast("string").alias("key"),
        F.to_json(
            F.struct(
                "account_id", "status", "tier", "region", "balance", "version",
                "updated_at", "deleted", "_change_type", "_commit_version",
                "_commit_timestamp",
            )
        ).alias("value"),
    )

    query = (
        payload.writeStream.format("kafka")
        .option("kafka.bootstrap.servers", BOOTSTRAP)
        .option("kafka.security.protocol", "SASL_SSL")
        .option("kafka.sasl.mechanism", "SCRAM-SHA-512")
        .option("kafka.sasl.jaas.config", jaas)
        # truststore PEM direto no ca.crt do cert-manager (kafka-clients >= 2.7)
        .option("kafka.ssl.truststore.type", "PEM")
        .option("kafka.ssl.truststore.location", KAFKA_CA_PATH)
        .option("topic", TOPIC)
        .option("checkpointLocation", CHECKPOINT)
        .trigger(processingTime=f"{TRIGGER_SECONDS} seconds")
        .start()
    )

    print(f"STREAM_STARTED topic={TOPIC} bootstrap={BOOTSTRAP} checkpoint={CHECKPOINT}")
    query.awaitTermination()
