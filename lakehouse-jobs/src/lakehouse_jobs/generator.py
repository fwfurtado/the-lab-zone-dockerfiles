"""Gerador de carga do lakehouse.

Loop infinito de rounds: cada round gera BATCH_SIZE ops (mix insert/update/delete
configurável), escolhe chaves de update/delete por amostragem Zipfiana (hot keys),
deduplica por chave (exigência do MERGE) e aplica UM `MERGE INTO` = UM commit Delta.

Manutenção (OPTIMIZE + VACUUM) roda DENTRO deste mesmo processo a cada
MAINTENANCE_EVERY rounds: Delta OSS em S3-compatível (Garage) não é seguro para
múltiplos writers concorrentes na mesma tabela (sem put-if-absent; a solução oficial,
S3DynamoDBLogStore, exige DynamoDB). Um processo = um writer = zero corrida no log.

VACUUM_RETAIN_HOURS também limita o CDF: arquivos de _change_data/ mais velhos que a
retenção são deletados -- se o streaming da etapa 1.5 ficar parado além disso, o
resume do checkpoint quebra com FileNotFound. Default 6h dá folga p/ kill/resume e
para os testes de pausa da Fase 2; reduzir só com o lag monitorado.
"""

import os
import time
from datetime import datetime, timezone

import numpy as np
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

TABLE_PATH = os.environ.get("TABLE_PATH", "s3a://lakehouse/tables/accounts")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "5000"))
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "30"))
INSERT_PCT = float(os.environ.get("INSERT_PCT", "0.15"))
UPDATE_PCT = float(os.environ.get("UPDATE_PCT", "0.75"))
DELETE_PCT = float(os.environ.get("DELETE_PCT", "0.10"))
ZIPF_S = float(os.environ.get("ZIPF_S", "1.1"))
MAINTENANCE_INTERVAL_HOURS = float(os.environ.get("MAINTENANCE_INTERVAL_HOURS", "1"))
VACUUM_RETAIN_HOURS = int(os.environ.get("VACUUM_RETAIN_HOURS", "6"))

STATUS = ["active", "blocked", "closed"]
TIER = ["bronze", "silver", "gold"]
REGIONS = [
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
]

assert abs(INSERT_PCT + UPDATE_PCT + DELETE_PCT - 1.0) < 1e-9, "mix deve somar 1.0"




def zipf_keys(rng: np.random.Generator, n: int, max_id: int) -> np.ndarray:
    """Amostra n account_ids em [0, max_id] com distribuição Zipf(s).

    rank 1 (mais quente) -> account_id 0. Rejection sampling: a Zipf é ilimitada,
    descartamos amostras acima do range e reamostramos.
    """
    out = np.empty(0, dtype=np.int64)
    while out.size < n:
        sample = rng.zipf(ZIPF_S, size=(n - out.size) * 2)
        sample = sample[sample <= max_id + 1]
        out = np.concatenate([out, sample[: n - out.size]])
    return out - 1  # rank 1 -> id 0


def build_ops(spark, round_no: int, max_existing_id: int):
    """Gera o batch do round. Seed = round_no => reproduzível bit a bit."""
    rng = np.random.default_rng(round_no)

    ops = rng.choice(
        ["insert", "update", "delete"],
        size=BATCH_SIZE,
        p=[INSERT_PCT, UPDATE_PCT, DELETE_PCT],
    )
    n_insert = int((ops == "insert").sum())
    n_update = int((ops == "update").sum())
    n_delete = BATCH_SIZE - n_insert - n_update

    # inserts: ids novos, contador monotônico acima do maior id existente
    insert_ids = np.arange(max_existing_id + 1, max_existing_id + 1 + n_insert)
    # updates: Zipf (hot keys concentram updates)
    update_ids = zipf_keys(rng, n_update, max_existing_id)
    # deletes: UNIFORME. Deletar via Zipf extinguia a cabeça da distribuição em
    # poucos rounds (hot key morria; updates nela viravam no-op) e o CDF observava
    # só a cauda morna -- distribuição achatada, inútil p/ estressar upsert quente.
    delete_ids = rng.integers(0, max_existing_id + 1, size=n_delete)

    rows = []
    it_insert = iter(insert_ids)
    it_update = iter(update_ids)
    it_delete = iter(delete_ids)
    now = datetime.now(timezone.utc)
    for seq, op in enumerate(ops):
        if op == "insert":
            acc = int(next(it_insert))
        elif op == "update":
            acc = int(next(it_update))
        else:
            acc = int(next(it_delete))
        rows.append(
            (
                seq,
                acc,
                str(op),  # numpy.str_ quebra a inferência de schema do createDataFrame
                str(rng.choice(STATUS)),
                str(rng.choice(TIER)),
                str(rng.choice(REGIONS)),
                round(float(rng.random() * 10000), 2),
                now,
            )
        )

    df = spark.createDataFrame(
        rows, ["seq", "account_id", "op", "status", "tier", "region", "balance", "updated_at"]
    )
    # MERGE exige fonte com no máx. 1 linha por chave; Zipf GARANTE colisão nas
    # quentes. Dedup: fica a última op gerada por chave (row_number por ordem de
    # geração). O mix realizado desvia do configurado -- logado abaixo; a
    # verificação de aceite usa os counts do CDF, não a intenção.
    from pyspark.sql.window import Window

    # seq explícita da geração: dedup determinístico ("última op gerada vence"),
    # sem expressão não-determinística no order by da window
    w = Window.partitionBy("account_id").orderBy(F.col("seq").desc())
    df = df.withColumn("_rn", F.row_number().over(w)).filter("_rn = 1").drop("_rn", "seq")
    return df, int(insert_ids[-1]) if n_insert > 0 else max_existing_id


def run_round(spark, round_no: int, max_id: int) -> int:
    ops_df, new_max_id = build_ops(spark, round_no, max_id)
    ops_df.createOrReplaceTempView("ops")

    realized = {r["op"]: r["n"] for r in ops_df.groupBy("op").agg(F.count("*").alias("n")).collect()}

    spark.sql(f"""
        MERGE INTO delta.`{TABLE_PATH}` t
        USING ops s
        ON t.account_id = s.account_id
        WHEN MATCHED AND s.op = 'delete' THEN DELETE
        WHEN MATCHED AND s.op = 'update' THEN UPDATE SET
            t.status = s.status,
            t.balance = s.balance,
            t.version = t.version + 1,
            t.updated_at = s.updated_at
        WHEN NOT MATCHED AND s.op = 'insert' THEN INSERT
            (account_id, status, tier, region, balance, version, updated_at)
            VALUES (s.account_id, s.status, s.tier, s.region, s.balance, 0, s.updated_at)
    """)
    # nota: update/delete em chave inexistente (já deletada) = no-op de propósito;
    # tier/region são imutáveis pós-insert (dims estáveis p/ o star-tree do Pinot).

    version = spark.sql(f"DESCRIBE HISTORY delta.`{TABLE_PATH}` LIMIT 1").collect()[0]["version"]
    print(
        f"ROUND={round_no} commit_version={version} "
        f"insert={realized.get('insert', 0)} update={realized.get('update', 0)} "
        f"delete={realized.get('delete', 0)}"
    )
    return new_max_id


def hours_since_last_maintenance(spark) -> float:
    """Lê a history da tabela p/ achar o último OPTIMIZE. O _delta_log é o relógio
    PERSISTENTE -- imune a restart do processo (o contador de rounds em memória
    zerava a cada restart e a manutenção nunca disparava de forma confiável)."""
    # DESCRIBE HISTORY não pode ser subquery (é comando utilitário, não relação) --
    # coletar o DataFrame e filtrar/ordenar no lado Python/DataFrame API
    from pyspark.sql import functions as F

    rows = (
        spark.sql(f"DESCRIBE HISTORY delta.`{TABLE_PATH}`")
        .filter(F.col("operation") == "OPTIMIZE")
        .orderBy(F.col("timestamp").desc())
        .limit(1)
        .collect()
    )
    if not rows:
        return float("inf")  # nunca houve manutenção -> roda já
    last = rows[0]["timestamp"].timestamp()
    return (time.time() - last) / 3600.0


def run_maintenance(spark, round_no: int):
    t0 = time.time()
    spark.sql(f"OPTIMIZE delta.`{TABLE_PATH}`")
    spark.sql(f"VACUUM delta.`{TABLE_PATH}` RETAIN {VACUUM_RETAIN_HOURS} HOURS")
    detail = spark.sql(f"DESCRIBE DETAIL delta.`{TABLE_PATH}`").collect()[0]
    print(
        f"MAINTENANCE round={round_no} took={time.time() - t0:.1f}s "
        f"numFiles={detail['numFiles']} sizeBytes={detail['sizeInBytes']}"
    )


def main():
    spark = (
        SparkSession.builder.appName("generator-accounts")
        # VACUUM < 7 dias exige desligar o safety check -- trade-off consciente:
        # time travel/CDF ficam fisicamente limitados a VACUUM_RETAIN_HOURS.
        .config("spark.databricks.delta.retentionDurationCheck.enabled", "false")
        .getOrCreate()
    )
    max_id = spark.sql(
        f"SELECT coalesce(max(account_id), -1) AS m FROM delta.`{TABLE_PATH}`"
    ).collect()[0]["m"]
    print(f"GENERATOR_START max_account_id={max_id} batch={BATCH_SIZE} "
          f"mix={INSERT_PCT}/{UPDATE_PCT}/{DELETE_PCT} zipf_s={ZIPF_S} "
          f"maintenance_interval_h={MAINTENANCE_INTERVAL_HOURS} vacuum_retain_h={VACUUM_RETAIN_HOURS}")

    round_no = 0
    while True:
        round_no += 1
        max_id = run_round(spark, round_no, max_id)
        # gatilho por RELÓGIO PERSISTENTE: sobrevive a restart do driver (o operator
        # ressubmete o app -- visto 11x em 39h; contador em memória perdia a conta)
        if hours_since_last_maintenance(spark) >= MAINTENANCE_INTERVAL_HOURS:
            run_maintenance(spark, round_no)
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
