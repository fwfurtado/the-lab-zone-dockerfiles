# JupyterLab para queries ad-hoc no lakehouse (substitui o pod sleep + pyspark REPL).
# Herda toda a matriz de jars da spark-delta -- NÃO adicionar jars aqui; mudanças de
# classpath pertencem à imagem base.
#
# jupyterlab 4.6.1: requires_python >=3.10 -- compatível com o Python 3.10 da base
# (mesma restrição que pinou o numpy em 2.2.6; bump de Python destrava os dois).

# 0.1.1 = build com Spark 4.1.1 (fix Delta 4.2.0 x Spark 4.1.2+);
# ajustar se o semver do CI atribuir outra tag
FROM ghcr.io/fwfurtado/spark-delta:0.2.0

LABEL org.opencontainers.image.title="spark-jupyter" \
      org.opencontainers.image.authors="Mimi <fwfurtado@gmail.com>" \
      org.opencontainers.image.vendor="The Lab Zone" \
      org.opencontainers.image.licenses="MIT"

USER root

# jupyterlab-sql-editor SEM o extra [all]: o extra instala pyspark do PyPI --
# um SEGUNDO Spark (jars proprios, versao divergente) dentro de uma imagem cujo
# Spark canonico e o /opt/spark. O pyspark dos notebooks vem do PYTHONPATH.
# O magic %%sparksql ainda exige load_ext no startup do IPython (fica no app).
RUN pip3 install --no-cache-dir jupyterlab==4.6.1 jupyterlab-sql-editor && \
    mkdir -p /home/spark/notebooks && \
    chown -R spark:spark /home/spark

# uid 185 (spark) não tem home na imagem base; Jupyter exige um gravável
ENV HOME=/home/spark

USER spark
WORKDIR /home/spark/notebooks

# O launch (pyspark com PYSPARK_DRIVER_PYTHON=jupyter-lab) fica no Deployment,
# junto dos confs canônicos -- a imagem não assume contexto de cluster.
