"""Runner de migrations do lakehouse (padrao Flyway, estado em Delta).

Estado: s3a://lakehouse/_meta/schema_migrations (id, applied_at). Aplica, em
ordem, toda migration do REGISTRY ausente do estado, registrando apos aplicar.
"""

import os

from pyspark.sql import SparkSession

from lakehouse_jobs.migrations import REGISTRY

STATE_TABLE = os.environ.get(
    "MIGRATIONS_STATE_TABLE", "s3a://lakehouse/_meta/schema_migrations"
)


def main():
    spark = SparkSession.builder.appName("lakehouse-migrate").getOrCreate()

    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS delta.`{STATE_TABLE}` (
          id          STRING,
          applied_at  TIMESTAMP
        ) USING DELTA
    """)

    applied = {
        r["id"] for r in spark.sql(f"SELECT id FROM delta.`{STATE_TABLE}`").collect()
    }

    pending = [(mid, fn) for mid, fn in REGISTRY if mid not in applied]
    print(f"MIGRATE_START applied={len(applied)} pending={len(pending)}")

    for mid, fn in pending:
        print(f"MIGRATE_APPLYING id={mid}")
        fn(spark)
        spark.sql(
            f"INSERT INTO delta.`{STATE_TABLE}` VALUES ('{mid}', current_timestamp())"
        )
        print(f"MIGRATE_APPLIED id={mid}")

    print(f"MIGRATE_OK total_applied_now={len(pending)}")
    spark.stop()
