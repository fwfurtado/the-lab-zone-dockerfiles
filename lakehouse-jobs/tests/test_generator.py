"""Testes do build_ops: dedup, mix, determinismo, cabeça Zipf viva."""

import os

os.environ.setdefault("BATCH_SIZE", "2000")

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from lakehouse_jobs.generator import build_ops


@pytest.fixture(scope="session")
def spark():
    s = SparkSession.builder.master("local[2]").appName("tests").getOrCreate()
    s.sparkContext.setLogLevel("ERROR")
    yield s
    s.stop()


def test_dedup_e_tipos(spark):
    df, new_max = build_ops(spark, round_no=1, max_existing_id=99_999)
    dupes = df.groupBy("account_id").count().filter("count > 1").count()
    assert dupes == 0, "MERGE exige no maximo 1 linha por chave na fonte"
    assert new_max > 99_999


def test_deterministico_por_seed(spark):
    a, _ = build_ops(spark, 7, 99_999)
    b, _ = build_ops(spark, 7, 99_999)
    a, b = a.drop("updated_at"), b.drop("updated_at")  # wall-clock proposital
    assert a.exceptAll(b).count() == 0 and b.exceptAll(a).count() == 0


def test_cabeca_zipf_recebe_update(spark):
    # deletes uniformes nao podem extinguir a cabeca: id 0 deve ser update
    # em praticamente todo round (licao do aceite da etapa 1.4)
    hits = 0
    for rnd in range(1, 6):
        df, _ = build_ops(spark, rnd, 99_999)
        hits += df.filter("op = 'update' AND account_id = 0").count()
    assert hits >= 4
