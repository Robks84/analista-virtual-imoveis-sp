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


@lru_cache(maxsize=1)
def periodo() -> dict:
    """Anos realmente carregados no banco (o período muda conforme as planilhas em data/raw)."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ini, fim = con.execute("SELECT MIN(ano), MAX(ano) FROM dim_tempo").fetchone()
    n = con.execute("SELECT COUNT(*) FROM fato_transacoes").fetchone()[0]
    con.close()
    return {"ano_inicio": ini, "ano_fim": fim, "transacoes": n,
            "texto": f"jan/{ini} a dez/{fim}"}


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
            "dominio": {**{k: sl["dominio"][k] for k in ("granularidade", "escopo_nao_responde")},
                        "periodo_carregado": periodo()["texto"]},
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


def buscar_local(nome: str) -> dict:
    """Acha o nome EXATO de bairro (dim_local.bairro) e/ou rua (fato_transacoes.logradouro) a partir
    do que o usuário digitou (com acento, minúsculas, abreviado ou com erro de digitação)."""
    import difflib
    alvo = re.sub(r"[^A-Z0-9 ]", " ", _norm(nome).upper())
    alvo = re.sub(r"\s+", " ", alvo).strip()
    if not alvo:
        return {"erro": "Informe um nome de bairro ou rua."}
    con = _conectar()
    bairros = [r[0] for r in con.execute("SELECT DISTINCT bairro FROM dim_local")]
    apelidos = {"JARDINS": ["JARDIM PAULISTA", "JARDIM AMERICA", "JARDIM EUROPA"]}
    achados = ([b for b in apelidos.get(alvo, []) if b in bairros]
               or [b for b in bairros if alvo in b]
               or difflib.get_close_matches(alvo, bairros, n=5, cutoff=0.75))
    palavras = [p for p in alvo.split() if p not in {"RUA", "R", "AV", "AVENIDA", "ALAMEDA", "AL"}]
    ruas = []
    if palavras:
        filtro = " AND ".join("logradouro LIKE ?" for _ in palavras)
        ruas = [{"logradouro": r[0], "transacoes": r[1]} for r in con.execute(
            f"SELECT logradouro, COUNT(*) FROM fato_transacoes WHERE {filtro} "
            "GROUP BY logradouro ORDER BY 2 DESC LIMIT 5", [f"%{p}%" for p in palavras])]
    con.close()
    return {"bairros": achados[:5], "ruas": ruas,
            "dica": "Use o valor exato com '=' (ex.: l.bairro = 'VILA MADALENA'). Se nada foi encontrado, "
                    "o local não existe com esse nome na base: diga isso ao usuário."}


def exemplos_sql() -> list[dict]:
    return semantic_layer().get("exemplos", [])


# ---------------------------------------------------------------------------
# Funções extras no SQLite (o SQLite não tem mediana nativa)
# ---------------------------------------------------------------------------
class _Mediana:
    def __init__(self):
        self.valores = []

    def step(self, v):
        if v is not None:
            self.valores.append(v)

    def finalize(self):
        if not self.valores:
            return None
        v = sorted(self.valores)
        m = len(v) // 2
        return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def _conectar() -> sqlite3.Connection:
    """Conexão SOMENTE LEITURA com as funções extras registradas."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.create_aggregate("MEDIAN", 1, _Mediana)
    return con


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
        con = _conectar()
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

    con = _conectar()                                            # somente leitura no nível do driver
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
_TEMPORAIS = {"ano", "mes", "mes_num", "mes_id", "ano_mes", "trimestre", "semestre", "nome_mes"}


def _tipo_valor(col: str) -> str:
    c = col.lower()
    if any(k in c for k in ("pct", "percent", "variacao", "crescimento", "taxa")):
        return "pct"
    if any(k in c for k in ("valor", "preco", "volume", "financiado", "m2", "ticket", "base_calculo")):
        return "rs"
    return "num"


def _fmt(v: float, tipo: str) -> str:
    if v is None:
        return ""
    if tipo == "pct":
        return f"{v:.1f}%".replace(".", ",")
    if tipo == "rs":
        for lim, suf in ((1e9, "bi"), (1e6, "mi"), (1e3, "mil")):
            if abs(v) >= lim:
                return f"R$ {v / lim:.1f} {suf}".replace(".", ",")
        return f"R$ {v:.0f}"
    return f"{v:,.0f}".replace(",", ".") if abs(v) >= 1 else f"{v:.2f}".replace(".", ",")


def _nome_bonito(col: str) -> str:
    nomes = {"preco_medio_m2": "Preço médio do m² (R$)", "preco_m2": "Preço do m² (R$)",
             "volume_financeiro": "Volume negociado (R$)", "valor_medio": "Valor médio (R$)",
             "qtd_vendas": "Número de vendas", "qtd_transacoes": "Número de transações",
             "qtd_compras_vendas": "Número de vendas", "pct_financiadas": "Vendas financiadas (%)",
             "variacao_pct": "Variação (%)", "ano": "Ano", "mes": "Mês", "zona": "Zona", "bairro": "Bairro"}
    return nomes.get(col.lower(), col.replace("_", " ").strip().capitalize())


def _grafico_series(colunas, linhas, cat_idx, tempo_idx, vi, titulo, plt):
    """Várias linhas no mesmo gráfico, uma por categoria (ex.: Moema x Tatuapé ano a ano)."""
    tipo = _tipo_valor(colunas[vi])
    periodos = sorted({r[tempo_idx] for r in linhas}, key=str)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    maximo = 0
    for cat in dict.fromkeys(r[cat_idx] for r in linhas):
        pontos = {r[tempo_idx]: r[vi] for r in linhas if r[cat_idx] == cat and r[vi] is not None}
        xs = [str(p) for p in periodos if p in pontos]
        ys = [pontos[p] for p in periodos if p in pontos]
        if not ys:
            continue
        ax.plot(xs, ys, marker="o", linewidth=2, label=str(cat).title())
        ax.annotate(_fmt(ys[-1], tipo), (xs[-1], ys[-1]), textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=8)
        maximo = max(maximo, max(ys))
    ax.set_ylim(0, maximo * 1.18 if maximo > 0 else 1)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt(v, tipo)))
    ax.set_ylabel(_nome_bonito(colunas[vi]))
    ax.set_xlabel(_nome_bonito(colunas[tempo_idx]))
    if len(periodos) > 6:
        ax.tick_params(axis="x", rotation=45)
    ax.legend(frameon=False, fontsize=9)
    if titulo:
        ax.set_title(titulo if len(titulo) <= 80 else titulo[:77] + "...", fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig


def make_chart(colunas: list[str], linhas: list[list[Any]], titulo: str | None = None):
    """Gráfico legível: eixo a partir do zero, valores escritos nos pontos/barras, nomes em português."""
    if not linhas or len(colunas) < 2 or len(linhas) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    if titulo:  # tira nomes técnicos do título: "preço médio do m² (preco_medio_m2)" -> "preço médio do m²"
        titulo = re.sub(r"\s*\([^)]*_[^)]*\)", "", titulo).strip()
    numericas = [i for i in range(len(colunas))
                 if all(isinstance(r[i], (int, float)) or r[i] is None for r in linhas)]
    tempo_idx = next((i for i in range(len(colunas)) if colunas[i].lower() in _TEMPORAIS), None)
    textos = [i for i in range(len(colunas)) if i not in numericas]
    # formato "longo" (categoria + período + valor, ex.: bairro, ano, preço): uma linha por categoria
    if tempo_idx is not None and textos and textos[0] != tempo_idx:
        cat_idx = textos[0]
        vals = [i for i in numericas if i not in (tempo_idx, cat_idx)]
        principais = [i for i in vals if not colunas[i].lower().startswith(("qtd", "n_", "amostra"))]
        if vals and len({r[cat_idx] for r in linhas}) <= 8:
            return _grafico_series(colunas, linhas, cat_idx, tempo_idx, (principais or vals)[0], titulo, plt)
    rotulo_idx = next((i for i in range(len(colunas)) if i not in numericas), tempo_idx if tempo_idx is not None else 0)
    candidatas = [i for i in numericas if i != rotulo_idx and colunas[i].lower() not in _TEMPORAIS]
    if not candidatas:
        return None
    # prefere a métrica principal (não a coluna de contagem de amostra), quando houver outra
    principais = [i for i in candidatas if not colunas[i].lower().startswith(("qtd", "n_", "amostra"))]
    vi = (principais or candidatas)[0]
    dados = linhas[:30]
    rotulos = [str(r[rotulo_idx]) for r in dados]
    valores = [r[vi] or 0 for r in dados]
    tipo = _tipo_valor(colunas[vi])
    temporal = colunas[rotulo_idx].lower() in _TEMPORAIS

    longos = not temporal and (len(dados) > 8 or max(len(x) for x in rotulos) > 12)
    fig, ax = plt.subplots(figsize=(8, 4.2 if not longos else 0.35 * len(dados) + 1.2))
    cor = "#2F6690"
    if temporal:
        ax.plot(rotulos, valores, color=cor, marker="o", linewidth=2)
        for x, y in zip(rotulos, valores):
            ax.annotate(_fmt(y, tipo), (x, y), textcoords="offset points", xytext=(0, 8),
                        ha="center", fontsize=8)
        ax.set_ylim(min(0, min(valores) * 1.15), max(valores) * 1.18 if max(valores) > 0 else 1)
    elif longos:
        ax.barh(rotulos[::-1], valores[::-1], color=cor)
        for y, v in zip(rotulos[::-1], valores[::-1]):
            ax.annotate(_fmt(v, tipo), (v, y), textcoords="offset points", xytext=(4, 0),
                        va="center", fontsize=8)
        ax.set_xlim(min(0, min(valores) * 1.15), max(valores) * 1.2 if max(valores) > 0 else 1)
    else:
        ax.bar(rotulos, valores, color=cor)
        for x, v in zip(rotulos, valores):
            ax.annotate(_fmt(v, tipo), (x, v), textcoords="offset points", xytext=(0, 4),
                        ha="center", fontsize=8)
        ax.set_ylim(min(0, min(valores) * 1.15), max(valores) * 1.18 if max(valores) > 0 else 1)
    eixo_valor = ax.xaxis if longos else ax.yaxis
    eixo_valor.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt(v, tipo)))
    (ax.set_xlabel if longos else ax.set_ylabel)(_nome_bonito(colunas[vi]))
    if not longos:
        ax.set_xlabel(_nome_bonito(colunas[rotulo_idx]))
        if len(dados) > 6:
            ax.tick_params(axis="x", rotation=45)
    if titulo:
        ax.set_title(titulo if len(titulo) <= 80 else titulo[:77] + "...", fontsize=11, loc="left")
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
        "name": "buscar_local",
        "description": "Acha o nome exato de um bairro ou rua citado na pergunta (aceita acento, minúscula e erro de digitação). Use ANTES de filtrar por bairro ou rua.",
        "parameters": {"type": "object", "properties": {"nome": {"type": "string"}}, "required": ["nome"]}}},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "Executa um SELECT somente leitura no banco SQLite e devolve colunas e linhas.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}},
]

FUNCOES = {"get_schema": get_schema, "get_metric": get_metric, "validate_sql": validate_sql, "run_sql": run_sql,
           "buscar_local": buscar_local}
