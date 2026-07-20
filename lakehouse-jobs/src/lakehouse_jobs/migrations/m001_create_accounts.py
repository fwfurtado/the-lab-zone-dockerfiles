"""m001: cria a tabela accounts (schema + properties) e faz o seed inicial.

Porte fiel do bootstrap_accounts.py validado na Fase 1. Idempotente nas duas
partes: CREATE IF NOT EXISTS + seed condicionado a tabela vazia -- rodar contra
a tabela viva é no-op (assim o runner pode registrá-la como aplicada em ambientes
que nasceram antes do runner existir).
"""

from pyspark.sql import functions as F


def apply(spark):
    TABLE_PATH = "s3a://lakehouse/tables/accounts"
    SEED_ROWS = 100_000

    # ---- criação da tabela (path-based -- sem catalog persistente, o path É a identidade) ----
    spark.sql(f"""
    CREATE TABLE IF NOT EXISTS delta.`{TABLE_PATH}` (
      account_id  BIGINT,
      status      STRING,
      tier        STRING,
      region      STRING,
      balance     DOUBLE,
      version     BIGINT,
      updated_at  TIMESTAMP
    )
    USING DELTA
    TBLPROPERTIES (
      'delta.enableChangeDataFeed' = 'true',
      'delta.logRetentionDuration' = 'interval 1 days',
      -- 6h casa com VACUUM_RETAIN_HOURS do gerador: esta property é o guard de
      -- leitura do CDF/time-travel (a promessa), o RETAIN do VACUUM é a prática
      'delta.deletedFileRetentionDuration' = 'interval 6 hours'
    )
    """)

    # ---- seed: 100k contas, geradas distribuído (spark.range), não em loop no driver ----
    STATUS = ["active", "blocked", "closed"]
    TIER = ["bronze", "silver", "gold"]
    REGIONS = [  # 27 UFs
        "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
        "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
        "SP", "SE", "TO",
    ]


    def rand_choice(values):
        arr = F.array(*[F.lit(v) for v in values])
        idx = (F.rand() * len(values)).cast("int")
        return F.element_at(arr, idx + 1)  # element_at é 1-indexed


    existing = spark.read.format("delta").load(TABLE_PATH).limit(1).count()
    if existing > 0:
        print(f"BOOTSTRAP_SKIPPED table_already_seeded")
    else:
        seed_df = (
            spark.range(0, SEED_ROWS)
            .withColumnRenamed("id", "account_id")
            .withColumn("status", rand_choice(STATUS))
            .withColumn("tier", rand_choice(TIER))
            .withColumn("region", rand_choice(REGIONS))
            .withColumn("balance", F.round(F.rand() * 10000, 2))
            .withColumn("version", F.lit(0).cast("long"))
            .withColumn("updated_at", F.current_timestamp())
        )
        seed_df.write.format("delta").mode("append").save(TABLE_PATH)

        count = spark.read.format("delta").load(TABLE_PATH).count()
        assert count == SEED_ROWS, f"esperava {SEED_ROWS} linhas, veio {count}"
        print(f"BOOTSTRAP_OK rows={count}")

