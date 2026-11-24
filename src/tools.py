"""
Ferramentas (tools) usadas pelos agentes via tool calling.

    get_schema()    -> Orquestrador: tabelas, colunas, relacionamentos e regras da camada semântica
    get_metric()    -> Orquestrador: definição de uma métrica (aceita sinônimos)
    validate_sql()  -> Agente SQL: guardrails antes de executar
    run_sql()       -> Agente SQL: execução SOMENTE LEITURA com LIMIT e timeout

Os guardrails ficam aqui, no código, e não no prompt: mesmo que a LLM "queira",
ela não consegue escrever no banco.
"""
from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "processed" / "itbi.db"
SEMANTIC_PATH = ROOT / "src" / "semantic_layer.yaml"

LIMITE_PADRAO = 200          # LIMIT aplicado quando a consulta não tem
LIMITE_MAXIMO = 1000         # nenhuma consulta devolve mais que isso
TIMEOUT_SEGUNDOS = 10        # tempo máximo de execução de uma consulta

PALAVRAS_PROIBIDAS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "TRUNCATE",
    "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX", "GRANT", "REVOKE", "MERGE", "UPSERT",
]


# ---------------------------------------------------------------------------
# Camada semântica
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def semantic_layer() -> dict:
    with open(SEMANTIC_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c)).strip()


def sinonimos_de_valores() -> dict:
    """Sinônimos que viram FILTRO (ex.: 'apartamento' -> u.categoria = '...'). Evita a LLM inventar valores."""
    return {k: v for k, v in semantic_layer()["sinonimos"].items() if "." in k}


def get_schema(tabelas: list[str] | None = None) -> dict:
    """Devolve a parte da camada semântica necessária para escrever SQL.

    tabelas: lista opcional de nomes; se vazia devolve todas.
    """
    sl = semantic_layer()
    todas = sl["tabelas"]
    if not tabelas:
        # resumo enxuto (economiza tokens): descrição + nomes das colunas de cada tabela
        return {
            "dominio": {k: sl["dominio"][k] for k in ("periodo_disponivel", "granularidade", "escopo_nao_responde")},
            "tabelas": {k: {"descricao": v["descricao"], "colunas": list(v["colunas"])} for k, v in todas.items()},
            "regras_de_negocio": sl["regras_de_negocio"],
            "metricas_disponiveis": {k: v["descricao"] for k, v in sl["metricas"].items()},
            "sinonimos_de_valores": sinonimos_de_valores(),
            "dica": "Para detalhes de colunas, chame get_schema com a lista de tabelas.",
        }
    escolhidas = {k: v for k, v in todas.items() if k in tabelas}
    return {
        "dominio": sl["dominio"],
        "tabelas": escolhidas,
        "relacionamentos": sl["relacionamentos"],
        "regras_de_negocio": sl["regras_de_negocio"],
        "sinonimos": sl["sinonimos"],
        "metricas_disponiveis": {k: v["descricao"] for k, v in sl["metricas"].items()},
    }


def get_metric(nome: str) -> dict:
    """Devolve a definição de uma métrica. Aceita o nome oficial ou um sinônimo
    (ex.: 'ticket médio' -> valor_medio)."""
    sl = semantic_layer()
    metricas = sl["metricas"]
    alvo = _norm(nome).replace(" ", "_")
    if alvo in metricas:
        return {"metrica": alvo, **metricas[alvo]}
    n = _norm(nome)
    for chave, sinonimos in sl["sinonimos"].items():
        if chave in metricas and any(_norm(s) == n or _norm(s) in n or n in _norm(s) for s in sinonimos):
            return {"metrica": chave, **metricas[chave]}
    return {"erro": f"Métrica '{nome}' não encontrada.", "metricas_disponiveis": list(metricas)}


def exemplos_sql() -> list[dict]:
    return semantic_layer().get("exemplos", [])


# ---------------------------------------------------------------------------
# Guardrails de SQL
# ---------------------------------------------------------------------------
def _sem_comentarios_e_strings(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


@lru_cache(maxsize=1)
def _schema_real() -> dict[str, set[str]]:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    tabelas = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")} for t in tabelas}
    con.close()
    return out


def _garantir_limit(sql: str) -> tuple[str, bool]:
    corpo = sql.strip().rstrip(";").strip()
    limpo = _sem_comentarios_e_strings(corpo)
    m = re.search(r"\bLIMIT\s+(\d+)\s*(?:OFFSET\s+\d+)?\s*$", limpo, flags=re.I)
    if m:
        if int(m.group(1)) > LIMITE_MAXIMO:
            corpo = re.sub(r"\bLIMIT\s+\d+", f"LIMIT {LIMITE_MAXIMO}", corpo, flags=re.I)
            return corpo, True
        return corpo, False
    return f"{corpo}\nLIMIT {LIMITE_PADRAO}", True


def validate_sql(sql: str) -> dict:
    """Valida o SQL SEM executar. Retorna {valido, erros, avisos, sql_final}."""
    erros, avisos = [], []
    if not sql or not sql.strip():
        return {"valido": False, "erros": ["SQL vazio."], "avisos": [], "sql_final": None}

    limpo = _sem_comentarios_e_strings(sql).strip().rstrip(";")
    upper = limpo.upper()

    # 1) somente leitura: uma única instrução, começando por SELECT ou WITH
    if ";" in limpo:
        erros.append("Apenas UMA instrução por consulta (encontrado ';' no meio do SQL).")
    if not re.match(r"^\s*(SELECT|WITH)\b", upper):
        erros.append("Somente consultas de leitura (SELECT/WITH) são permitidas.")
    for p in PALAVRAS_PROIBIDAS:
        if re.search(rf"\b{p}\b", upper):
            erros.append(f"Comando proibido: {p}. O acesso é somente leitura.")

    # 2) tabelas e colunas existem?
    schema = _schema_real()
    ctes = {c.lower() for c in re.findall(r"\b(\w+)\s+AS\s*\(", limpo, flags=re.I)}
    usadas = {t.lower() for t in re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)", limpo, flags=re.I)}
    for t in usadas - ctes:
        if t not in schema:
            erros.append(f"Tabela inexistente: {t}. Tabelas válidas: {', '.join(sorted(schema))}.")

    # 3) regras de negócio da camada semântica (avisos que o agente deve considerar)
    if "PRECO_M2" in upper and "ELEGIVEL_M2" not in upper:
        avisos.append("Preço do m² deve ter o filtro f.elegivel_m2 = 1 (regra de negócio).")
    if re.search(r"\b(SUM|AVG)\s*\(\s*\w*\.?VALOR_TRANSACAO", upper) and "VALOR_VALIDO" not in upper:
        avisos.append("Métricas de valor devem filtrar f.valor_valido = 1 (regra de negócio).")
    if "BAIRRO_INFORMADO" in upper:
        avisos.append("Use dim_local.bairro para bairro; bairro_informado é incompleto.")
    if "/ AREA_CONSTRUIDA_M2" in upper.replace("F.", "") and "ELEGIVEL_M2" not in upper:
        avisos.append("Não divida por área construída sem f.elegivel_m2 = 1; use f.preco_m2.")

    if erros:
        return {"valido": False, "erros": erros, "avisos": avisos, "sql_final": None}

    # 4) LIMIT padrão
    sql_final, limit_adicionado = _garantir_limit(sql)
    if limit_adicionado:
        avisos.append("LIMIT aplicado/ajustado automaticamente pelo guardrail.")

    # 5) o SQLite consegue compilar? (EXPLAIN não executa a consulta)
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        con.execute(f"EXPLAIN {sql_final}")
        con.close()
    except sqlite3.Error as e:
        return {"valido": False, "erros": [f"Erro de sintaxe/semântica SQL: {e}"], "avisos": avisos,
                "sql_final": None}

    return {"valido": True, "erros": [], "avisos": avisos, "sql_final": sql_final}


def run_sql(sql: str, max_linhas_retorno: int = LIMITE_MAXIMO) -> dict:
    """Executa uma consulta validada. Conexão read-only + timeout."""
    v = validate_sql(sql)
    if not v["valido"]:
        return {"ok": False, "erro": " | ".join(v["erros"]), "sql": sql}
    sql_final = v["sql_final"]

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)   # somente leitura no nível do driver
    con.execute("PRAGMA query_only = ON")
    inicio = time.time()

    def _timeout() -> int:   # chamado periodicamente pelo SQLite; != 0 aborta a consulta
        return 1 if time.time() - inicio > TIMEOUT_SEGUNDOS else 0

    con.set_progress_handler(_timeout, 10_000)
    try:
        cur = con.execute(sql_final)
        colunas = [d[0] for d in cur.description]
        linhas = cur.fetchmany(max_linhas_retorno)
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "interrupted" in msg:
            msg = f"Consulta abortada por timeout ({TIMEOUT_SEGUNDOS}s). Simplifique ou agregue mais."
        return {"ok": False, "erro": msg, "sql": sql_final}
    except sqlite3.Error as e:
        return {"ok": False, "erro": str(e), "sql": sql_final}
    finally:
        con.close()

    return {
        "ok": True,
        "sql": sql_final,
        "colunas": colunas,
        "linhas": [list(r) for r in linhas],
        "n_linhas": len(linhas),
        "tempo_s": round(time.time() - inicio, 3),
        "avisos": v["avisos"],
    }


# ---------------------------------------------------------------------------
# Opcional: gráfico simples para o Agente Analista / interface
# ---------------------------------------------------------------------------
def make_chart(colunas: list[str], linhas: list[list[Any]]):
    """Gera um gráfico matplotlib quando o resultado tem 1 coluna de rótulo + 1 numérica."""
    if not linhas or len(colunas) < 2 or len(linhas) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    idx_num = [i for i in range(1, len(colunas)) if all(isinstance(r[i], (int, float)) or r[i] is None for r in linhas)]
    if not idx_num:
        return None
    i = idx_num[-1]
    rotulos = [str(r[0]) for r in linhas[:30]]
    valores = [r[i] or 0 for r in linhas[:30]]
    fig, ax = plt.subplots(figsize=(8, 4))
    temporal = colunas[0].lower() in {"mes", "mes_id", "ano_mes", "trimestre", "ano"}
    (ax.plot if temporal else ax.bar)(rotulos, valores, color="#2F6690")
    ax.set_ylabel(colunas[i])
    ax.set_xlabel(colunas[0])
    ax.tick_params(axis="x", rotation=45)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Especificação das ferramentas no formato de tool calling (OpenAI-compatível)
# ---------------------------------------------------------------------------
TOOLS_ORQUESTRADOR = [
    {"type": "function", "function": {
        "name": "get_schema",
        "description": "Consulta a camada semântica: tabelas, colunas, relacionamentos, sinônimos e regras de negócio.",
        "parameters": {"type": "object", "properties": {
            "tabelas": {"type": "array", "items": {"type": "string"},
                        "description": "Tabelas de interesse. Vazio = todas."}}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_metric",
        "description": "Busca a definição oficial (SQL + filtro) de uma métrica pelo nome ou sinônimo.",
        "parameters": {"type": "object", "properties": {
            "nome": {"type": "string", "description": "Ex.: 'preço do m²', 'ticket médio', 'volume_financeiro'"}},
            "required": ["nome"]}}},
]

TOOLS_SQL = [
    {"type": "function", "function": {
        "name": "validate_sql",
        "description": "Valida o SQL sem executar (somente leitura, tabelas válidas, regras de negócio, sintaxe).",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "Executa um SELECT somente leitura no banco SQLite e devolve colunas e linhas.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}},
]

FUNCOES = {"get_schema": get_schema, "get_metric": get_metric, "validate_sql": validate_sql, "run_sql": run_sql}
