"""Testes da peça 2: o indexer lê as conclusões irmãs e enriquece o payload.

Dois contratos:
  1. Junção por `dedup_key`; ausência de conclusão degrada suave (sem campos).
  2. O `_common` não arrasta o stack de embedding para quem só quer o parser —
     o pod do classificador não deve carregar o onnxruntime.
"""

import os
import subprocess
import sys
import textwrap

from triage_indexer import _common
from triage_indexer._common import Report, base_payload, load_conclusions
from triage_indexer.conclusions import Diagnosed, Inconclusive, to_markdown


def _cfg(tmp_path):
    return _common.Config(
        qdrant_url="http://stub",
        api_key=None,
        collection="c",
        model="m",
        repo_dir=str(tmp_path),
        globs=["triage/**/*.md"],
        conclusion_globs=["conclusions/**/*.md"],
        batch=8,
        run_id="run1",
        count_path="/tmp/x",
        payload_indexes={},
    )


def _write_conclusion(tmp_path, dedup, c):
    p = tmp_path / "conclusions" / "data" / "Alert" / f"2026__{dedup}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(to_markdown(c, dedup), encoding="utf-8")
    return p


def _report(dedup, **fm):
    return Report(dedup_key=dedup, front_matter=fm, body="# corpo", path="p")


# --------------------------------------------------------------------------
# load_conclusions: junção por dedup_key
# --------------------------------------------------------------------------


def test_carrega_conclusao_diagnosed(tmp_path):
    _write_conclusion(
        tmp_path, "aaa", Diagnosed(verdict="pod X negado pela CNP", confidence="high", rationale="r")
    )
    got = load_conclusions(str(tmp_path), ["conclusions/**/*.md"])

    assert got == {"aaa": {"outcome": "diagnosed", "verdict": "pod X negado pela CNP", "confidence": "high"}}


def test_carrega_conclusao_inconclusive_sem_confidence(tmp_path):
    _write_conclusion(tmp_path, "bbb", Inconclusive(reason="sem RBAC", rationale="r"))
    got = load_conclusions(str(tmp_path), ["conclusions/**/*.md"])

    assert got == {"bbb": {"outcome": "inconclusive"}}
    assert "confidence" not in got["bbb"]  # a incoerência é inexpressável


def test_conclusao_invalida_avisa_e_nao_derruba(tmp_path, capsys):
    ok = _write_conclusion(tmp_path, "aaa", Diagnosed(verdict="v", confidence="low", rationale="r"))
    ruim = ok.parent / "2026__zzz.md"
    ruim.write_text('---\nschema: 1\ndedup_key: "zzz"\noutcome: maybe\n---\n\n# x', encoding="utf-8")

    got = load_conclusions(str(tmp_path), ["conclusions/**/*.md"])

    assert "aaa" in got and "zzz" not in got  # a boa entra, a ruim não
    assert "conclusão inválida" in capsys.readouterr().out


def test_conclusao_sem_dedup_key_avisa(tmp_path, capsys):
    p = tmp_path / "conclusions" / "x.md"
    p.parent.mkdir(parents=True)
    p.write_text('---\noutcome: diagnosed\nverdict: "v"\nconfidence: high\n---\n\n# x', encoding="utf-8")

    assert load_conclusions(str(tmp_path), ["conclusions/**/*.md"]) == {}
    assert "sem dedup_key" in capsys.readouterr().out


def test_corpus_sem_conclusoes_devolve_vazio(tmp_path):
    assert load_conclusions(str(tmp_path), ["conclusions/**/*.md"]) == {}


# --------------------------------------------------------------------------
# base_payload: enriquecimento e degradação suave
# --------------------------------------------------------------------------


def test_payload_ganha_campos_da_conclusao(tmp_path):
    p = base_payload(
        _cfg(tmp_path),
        _report("aaa", namespace="data"),
        {"outcome": "diagnosed", "verdict": "causa X", "confidence": "high"},
    )
    md = p["metadata"]
    assert md["outcome"] == "diagnosed"
    assert md["verdict"] == "causa X"
    assert md["confidence"] == "high"
    assert md["dedup_key"] == "aaa"  # os fatos do relatório seguem lá


def test_payload_sem_conclusao_nao_inventa_campos(tmp_path):
    """Degradação suave: relatório recém-triado, ainda não classificado."""
    md = base_payload(_cfg(tmp_path), _report("aaa"))["metadata"]

    assert "outcome" not in md
    assert "verdict" not in md
    assert "confidence" not in md
    assert md["confirmation"] == "unverified"  # esse continua vindo do relatório


def test_payload_inconclusive_nao_traz_confidence(tmp_path):
    md = base_payload(_cfg(tmp_path), _report("bbb"), {"outcome": "inconclusive"})["metadata"]

    assert md["outcome"] == "inconclusive"
    assert "confidence" not in md
    assert "verdict" not in md


def test_conclusao_nao_sobrescreve_fatos_do_relatorio(tmp_path):
    md = base_payload(
        _cfg(tmp_path), _report("aaa", namespace="data"), {"outcome": "diagnosed"}
    )["metadata"]
    assert md["namespace"] == "data"
    assert md["run_id"] == "run1"


# --------------------------------------------------------------------------
# Os defaults escopam os prefixos (o glob do relatório não varre conclusions/)
# --------------------------------------------------------------------------


def test_default_dos_globs_escopa_os_prefixos(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://stub")
    for k in ("DOC_GLOBS", "CONCLUSION_GLOBS"):
        monkeypatch.delenv(k, raising=False)

    cfg = _common.Config.from_env(collection_default="c", payload_indexes={})
    assert cfg.globs == ["triage/**/*.md"]
    assert cfg.conclusion_globs == ["conclusions/**/*.md"]


# --------------------------------------------------------------------------
# O classificador não carrega o stack de embedding
# --------------------------------------------------------------------------


def test_importar_o_classificador_nao_puxa_fastembed_nem_qdrant():
    """O pod do classificador não deve carregar onnxruntime (centenas de MB).

    Roda em SUBPROCESSO limpo, sem os stubs do conftest: se `_common` importasse
    `mcp_server_qdrant` no topo, este import falharia (o pacote não está no
    ambiente de teste). Que ele passe é a prova de que os imports são preguiçosos.
    """
    code = textwrap.dedent(
        """
        import sys
        import triage_indexer.classifier  # noqa: F401
        assert "mcp_server_qdrant" not in sys.modules, "arrastou o mcp-server-qdrant"
        assert "qdrant_client" not in sys.modules, "arrastou o qdrant-client"
        assert "fastembed" not in sys.modules, "arrastou o fastembed"
        print("ok")
        """
    )
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"import não é preguiçoso:\n{r.stderr}"
    assert "ok" in r.stdout


# --------------------------------------------------------------------------
# O enriquecimento é observável: 0 conclusões não pode ser silencioso
# --------------------------------------------------------------------------


def test_reporta_quantos_relatorios_foram_enriquecidos(capsys):
    reports = [_report("aaa"), _report("bbb"), _report("ccc")]
    conclusions = {"aaa": {"outcome": "diagnosed"}, "bbb": {"outcome": "inconclusive"}}

    _common._report_enrichment(reports, conclusions)

    out = capsys.readouterr().out
    assert "2 carregadas" in out
    assert "2/3 relatórios enriquecidos" in out


def test_zero_conclusoes_com_relatorios_e_aviso_barulhento(capsys):
    """No Qdrant, 'não enriqueceu' e 'não classificado ainda' têm o MESMO sintoma
    (payload sem outcome). Só o log distingue — e o sintoma aparece dias depois,
    num filtro que não casa nada."""
    _common._report_enrichment([_report("aaa")], {})

    out = capsys.readouterr().out
    assert "AVISO: 0 conclusões" in out
    assert "classificador rodou?" in out


def test_conclusao_orfa_avisa(capsys):
    """Conclusão sem relatório irmão: corpus dessincronizado."""
    _common._report_enrichment([_report("aaa")], {"aaa": {}, "zzz": {}})

    assert "1 conclusões sem relatório irmão" in capsys.readouterr().out


def test_corpus_vazio_nao_polui_o_log(capsys):
    _common._report_enrichment([], {})
    assert capsys.readouterr().out == ""
