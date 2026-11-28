"""Testes dos guardrails e das ferramentas (não precisam de chave de API).

    pytest -q tests/
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from tools import (LIMITE_MAXIMO, LIMITE_PADRAO, buscar_local, get_metric, get_schema, make_chart,  # noqa: E402
                   run_sql, validate_sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM fato_transacoes",
    "UPDATE fato_transacoes SET valor_transacao = 0",
    "INSERT INTO dim_tempo VALUES (1,'x',1,1,'x',1,1)",
    "DROP TABLE fato_transacoes",
    "ALTER TABLE dim_tempo ADD COLUMN x INT",
    "SELECT 1; DROP TABLE dim_tempo",
    "PRAGMA table_info(fato_transacoes)",
    "ATTACH DATABASE 'x.db' AS x",
])
def test_bloqueia_escrita(sql):
    v = validate_sql(sql)
    assert not v["valido"]
    assert not run_sql(sql)["ok"]


def test_comentario_nao_engana_guardrail():
    assert not validate_sql("SELECT 1 /* inofensivo */ ; DELETE FROM dim_tempo")["valido"]


def test_palavra_proibida_dentro_de_string_e_permitida():
    v = validate_sql("SELECT COUNT(*) FROM dim_natureza WHERE descricao = 'UPDATE'")
    assert v["valido"]


def test_limit_padrao_aplicado():
    r = run_sql("SELECT transacao_id FROM fato_transacoes")
    assert r["ok"] and r["n_linhas"] == LIMITE_PADRAO


def test_limit_excessivo_reduzido():
    r = run_sql("SELECT transacao_id FROM fato_transacoes LIMIT 999999")
    assert r["ok"] and r["n_linhas"] == LIMITE_MAXIMO


def test_tabela_inexistente():
    v = validate_sql("SELECT * FROM clientes")
    assert not v["valido"] and "inexistente" in v["erros"][0]


def test_coluna_inexistente_vira_erro_explicavel():
    v = validate_sql("SELECT preco FROM fato_transacoes")
    assert not v["valido"] and "no such column" in v["erros"][0]


def test_aviso_regra_de_negocio_m2():
    v = validate_sql("SELECT AVG(preco_m2) FROM fato_transacoes")
    assert v["valido"] and any("elegivel_m2" in a for a in v["avisos"])


def test_timeout(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "TIMEOUT_SEGUNDOS", 0.5)
    r = run_sql("SELECT COUNT(*) FROM fato_transacoes a, fato_transacoes b")
    assert not r["ok"] and "timeout" in r["erro"]


def test_get_metric_por_sinonimo():
    assert get_metric("ticket médio")["metrica"] == "valor_medio"
    assert get_metric("preço do metro quadrado")["metrica"] == "preco_medio_m2"
    assert "erro" in get_metric("churn")


def test_get_schema_filtra_tabelas():
    s = get_schema(["dim_local"])
    assert list(s["tabelas"]) == ["dim_local"] and s["regras_de_negocio"]


def test_consulta_valida_executa():
    r = run_sql("SELECT t.ano, COUNT(*) AS qtd FROM fato_transacoes f "
                "JOIN dim_tempo t ON f.mes_id = t.mes_id GROUP BY t.ano")
    assert r["ok"] and r["colunas"] == ["ano", "qtd"] and len(r["linhas"]) >= 2


def test_mediana_disponivel():
    r = run_sql("SELECT MEDIAN(valor_transacao) AS mediana FROM fato_transacoes WHERE valor_valido = 1")
    assert r["ok"] and r["linhas"][0][0] > 0


def test_buscar_local_aceita_acento_minuscula_e_erro():
    assert "VILA MADALENA" in buscar_local("vila madalena")["bairros"]
    assert "MOOCA" in buscar_local("Moóca")["bairros"]
    assert "PINHEIROS" in buscar_local("Pinheros")["bairros"]
    assert any(r["logradouro"] == "RUA TEODORO SAMPAIO" for r in buscar_local("Teodoro Sampaio")["ruas"])


def test_buscar_local_inexistente_nao_inventa():
    r = buscar_local("Xyzwqk Inexistente")
    assert r["bairros"] == [] and r["ruas"] == []


def test_grafico_comeca_no_zero():
    fig = make_chart(["ano", "preco_medio_m2"], [[2019, 3900.0], [2025, 5400.0]], "teste")
    assert fig.axes[0].get_ylim()[0] == 0
