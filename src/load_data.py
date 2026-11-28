"""
Carga + limpeza + modelagem (fato + dimensões) + testes de qualidade.

Fonte: Prefeitura de São Paulo / Secretaria da Fazenda
"Dados das Transações Imobiliárias com recolhimento de ITBI" (2024 e 2025).

Uso:
    python src/load_data.py

Gera: data/processed/itbi.db (SQLite) e data/processed/relatorio_qualidade.txt
"""
from __future__ import annotations

import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "processed" / "itbi.db"
REPORT_PATH = ROOT / "data" / "processed" / "relatorio_qualidade.txt"
REF_LOCAL = ROOT / "data" / "reference" / "dim_local.csv"

# As abas mensais têm sempre as mesmas 28 colunas, na mesma ordem.
# Os cabeçalhos, porém, variam (JAN-2024 e OUT-2024 vêm SEM cabeçalho e algumas
# abas repetem "ACC (IPTU)" no lugar de "Descrição do padrão"). Por isso a
# leitura é posicional, com nomes padronizados aqui.
COLUNAS = [
    "sql_cadastro", "logradouro", "numero", "complemento", "bairro", "referencia",
    "cep", "natureza_transacao", "valor_transacao", "data_transacao",
    "valor_venal_referencia", "proporcao_transmitida_pct",
    "valor_venal_ref_proporcional", "base_calculo", "tipo_financiamento",
    "valor_financiado", "cartorio", "matricula", "situacao_sql",
    "area_terreno_m2", "testada_m", "fracao_ideal", "area_construida_m2",
    "uso_iptu", "desc_uso", "padrao_iptu", "desc_padrao", "acc_iptu",
]
MESES = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
         "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}
NOME_MES = {v: k.capitalize() for k, v in MESES.items()}
SITUACOES_VALIDAS = {"Ativo Predial", "Ativo Territorial", "Cancelado Predial",
                     "Cancelado Territorial", "Não_Existe_Exercício"}

# Usos residenciais "de unidade" (usados no cálculo de preço do m²)
USOS_RESIDENCIAIS = {10, 12, 14, 20, 21, 22, 25}

log_lines: list[str] = []


def log(msg: str = "") -> None:
    print(msg)
    log_lines.append(msg)


# ---------------------------------------------------------------------------
# 1. Leitura
# ---------------------------------------------------------------------------
def ler_planilhas() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    arquivos = sorted(RAW_DIR.glob("*.xlsx"))
    if not arquivos:
        sys.exit(f"Nenhum .xlsx encontrado em {RAW_DIR}")
    frames, usos, padroes = [], None, None
    for arq in arquivos:
        log(f"Lendo {arq.name} ...")
        abas = pd.read_excel(arq, sheet_name=None, header=None, engine="calamine")
        for nome, df in abas.items():
            m = re.fullmatch(r"([A-Z]{3})-(\d{4})", nome.strip())
            if m:
                df = df.iloc[:, :28].copy()
                df.columns = COLUNAS
                if str(df.iloc[0, 0]).startswith("N°"):   # cabeçalho presente
                    df = df.iloc[1:]
                df = df.dropna(how="all")
                df["ano_ref"] = int(m.group(2))
                df["mes_ref"] = MESES[m.group(1)]
                frames.append(df)
                log(f"   {nome}: {len(df):>6} linhas")
            elif nome.startswith("Tabela de USOS") and usos is None:
                usos = df.iloc[1:, :2].dropna()
                usos.columns = ["uso_id", "descricao"]
            elif nome.startswith("Tabela de PADR") and padroes is None:
                padroes = df.iloc[1:, :2].dropna()
                padroes.columns = ["padrao_id", "descricao"]
    bruto = pd.concat(frames, ignore_index=True)
    log(f"Total bruto: {len(bruto)} linhas")
    return bruto, usos, padroes


# ---------------------------------------------------------------------------
# 2. Limpeza
# ---------------------------------------------------------------------------
def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


ABREVIACOES = {
    r"\bN S\b": "NOSSA SENHORA", r"\bNSA SRA\b": "NOSSA SENHORA",
    r"\bVL\b": "VILA", r"\bPRQ\b": "PARQUE", r"\bREC\b": "RECANTO", r"\bJD\b": "JARDIM", r"\bJDM\b": "JARDIM", r"\bSTO\b": "SANTO",
    r"\bSTA\b": "SANTA", r"\bPQ\b": "PARQUE", r"\bPQUE\b": "PARQUE", r"\bCID\b": "CIDADE",
    r"\bCHAC\b": "CHACARA", r"\bS\b": "SAO", r"\bN\b": "NOSSA", r"\bSRA\b": "SENHORA",
    r"\bCJ\b": "CONJUNTO", r"\bRES\b": "RESIDENCIAL",
}
LIXO_BAIRRO = re.compile(
    r"^(TORRE|TR|BLOCO|BL|AP|APTO|APT|APART|ED|EDIF|EDIFICIO|COND|CONDOMINIO|LOTE|LT|"
    r"QUADRA|QD|CASA|CS|UNID|UNIDADE|ANDAR|SALA|SL|LOJA|VAGA|BOX|FASE|SETOR|MODULO|"
    r"GLEBA|RESIDENCIAL|RESID|EMPREENDIMENTO|SUBSOLO|TERREO|COBERTURA|DUPLEX|STUDIO)\b"
)


def limpar_bairro(b) -> str | None:
    if not isinstance(b, str):
        return None
    b = sem_acento(b.upper().strip())
    b = re.sub(r"[^A-Z0-9 ]", " ", b)
    b = re.sub(r"\s+", " ", b).strip()
    if not b or re.search(r"\d", b) or LIXO_BAIRRO.match(b) or len(b) < 3:
        return None
    for padrao, subst in ABREVIACOES.items():
        b = re.sub(padrao, subst, b)
    return b


TIPOS_LOGRADOURO = {"R": "RUA", "AV": "AVENIDA", "AL": "ALAMEDA", "PCA": "PRACA", "PC": "PRACA",
                    "EST": "ESTRADA", "TV": "TRAVESSA", "VD": "VIADUTO", "ROD": "RODOVIA", "LGO": "LARGO",
                    "VL": "VILA", "PQ": "PARQUE", "PSG": "PASSAGEM", "VIELA": "VIELA"}


TITULOS_LOGRADOURO = {
    "DR": "DOUTOR", "PROF": "PROFESSOR", "CEL": "CORONEL", "STA": "SANTA", "STO": "SANTO", "PDE": "PADRE",
    "NSRA": "NOSSA SENHORA", "BRIG": "BRIGADEIRO", "ENG": "ENGENHEIRO", "SEN": "SENADOR", "CAP": "CAPITAO",
    "PRES": "PRESIDENTE", "MIN": "MINISTRO", "CONS": "CONSELHEIRO", "GAL": "GENERAL", "MARQ": "MARQUES",
    "MAJ": "MAJOR", "MAL": "MARECHAL", "VER": "VEREADOR", "VISC": "VISCONDE", "DES": "DESEMBARGADOR",
    "DEP": "DEPUTADO", "TEN": "TENENTE", "GOV": "GOVERNADOR", "CMTE": "COMANDANTE", "ALM": "ALMIRANTE",
}


def limpar_logradouro(l) -> str | None:
    """Nome da rua padronizado (sem número/complemento): 'R DR JOSE ELIAS' -> 'RUA DR JOSE ELIAS'."""
    if not isinstance(l, str) or not l.strip():
        return None
    l = sem_acento(l.upper().strip())
    l = re.sub(r"[^A-Z0-9 ]", " ", l)
    l = re.sub(r"\s+", " ", l).strip()
    partes = l.split(" ", 1)
    if partes[0] in TIPOS_LOGRADOURO:
        partes[0] = TIPOS_LOGRADOURO[partes[0]]
    if len(partes) > 1:  # título logo após o tipo: 'AV BRIG FARIA LIMA' -> 'AVENIDA BRIGADEIRO FARIA LIMA'
        resto = partes[1].split(" ", 1)
        resto[0] = TITULOS_LOGRADOURO.get(resto[0], resto[0])
        partes[1] = " ".join(resto)
    return " ".join(partes)


def zona_por_cep(cep) -> str:
    """Zona aproximada a partir das faixas de CEP da capital (Correios)."""
    try:
        c = int(cep)
    except (TypeError, ValueError):
        return "Não informada"
    p = c // 100000          # 3 primeiros dígitos (ex.: 05718000 -> 57)
    if 1000 <= c // 1000 <= 1599:
        return "Centro"
    if 2000 <= c // 1000 <= 2999:
        return "Norte"
    if 3000 <= c // 1000 <= 3999 or 8000 <= c // 1000 <= 8499:
        return "Leste"
    if 4000 <= c // 1000 <= 4999 or 5700 <= c // 1000 <= 5899:
        return "Sul"
    if 5100 <= c // 1000 <= 5299:
        return "Norte"          # Pirituba, Perus, Jaraguá
    if 5000 <= c // 1000 <= 5699:
        return "Oeste"
    return "Não informada"


def grupo_natureza(desc: str) -> str:
    d = desc.lower()
    if "compra e venda" in d and "cessão" not in d and "compromisso" not in d:
        return "Compra e venda"
    if any(k in d for k in ["arremata", "adjudica", "sentença", "embargos", "alienação fiduciária por inadimplemento"]):
        return "Judicial / retomada"
    if any(k in d for k in ["capital", "cisão", "incorpora", "pessoa jurídica", "fusão"]):
        return "Societária"
    if any(k in d for k in ["partilha", "meação", "quinhão", "hereditári", "usufruto"]):
        return "Família / sucessão"
    return "Outras"


def categoria_uso(uso_id: int) -> str:
    if uso_id == 0:
        return "Terreno"
    if uso_id in {10, 12, 13, 14}:
        return "Residencial horizontal (casa)"
    if uso_id in {20, 21, 22, 25}:
        return "Residencial vertical (apartamento/flat)"
    if uso_id in {23, 24, 62, 63}:
        return "Garagem"
    if uso_id in {30, 31, 32, 40, 41, 42, 43, 85}:
        return "Comercial / escritório / loja"
    if uso_id in {50, 51, 60, 61, 64, 26}:
        return "Industrial / galpão / serviço"
    return "Institucional / outros"


def limpar(bruto: pd.DataFrame) -> pd.DataFrame:
    df = bruto.copy()
    n0 = len(df)

    # 2.1 Linhas desalinhadas / cabeçalhos repetidos no meio da aba
    df["valor_transacao"] = pd.to_numeric(df["valor_transacao"], errors="coerce")
    ok = (df["situacao_sql"].isin(SITUACOES_VALIDAS) & df["valor_transacao"].notna()
          & pd.to_numeric(df["sql_cadastro"], errors="coerce").notna())
    log(f"[limpeza] linhas desalinhadas/cabeçalho repetido removidas: {(~ok).sum()}")
    df = df[ok].copy()
    zero = df["valor_transacao"] <= 0
    log(f"[limpeza] transações sem valor declarado (valor <= 0) removidas: {zero.sum()}")
    df = df[~zero].copy()

    # 2.2 Tipos
    num = ["valor_venal_referencia", "proporcao_transmitida_pct", "valor_venal_ref_proporcional",
           "base_calculo", "valor_financiado", "area_terreno_m2", "testada_m", "fracao_ideal",
           "area_construida_m2", "uso_iptu", "padrao_iptu", "acc_iptu", "cep"]
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["data_transacao"] = pd.to_datetime(df["data_transacao"], errors="coerce")
    df["sql_cadastro"] = (df["sql_cadastro"].astype(str).str.replace(r"\.0$", "", regex=True)
                          .str.zfill(11))
    df["setor_fiscal"] = df["sql_cadastro"].str[:3]

    # 2.3 Duplicatas exatas (mesma DTI repetida no mesmo mês)
    chave = [c for c in COLUNAS] + ["ano_ref", "mes_ref"]
    dup = df.duplicated(subset=chave)
    log(f"[limpeza] duplicatas exatas removidas: {dup.sum()}")
    df = df[~dup].copy()

    # 2.4 Valores inválidos -> NULL
    neg = (df["valor_financiado"] < 0).sum()
    df.loc[df["valor_financiado"] < 0, "valor_financiado"] = None
    df["valor_financiado"] = df["valor_financiado"].fillna(0)
    log(f"[limpeza] valor_financiado negativo -> NULL/0: {neg}")
    acc_ruim = ((df["acc_iptu"] < 1800) | (df["acc_iptu"] > 2026)).sum()
    df.loc[(df["acc_iptu"] < 1800) | (df["acc_iptu"] > 2026), "acc_iptu"] = None
    log(f"[limpeza] ano de construção fora de 1800-2026 -> NULL: {acc_ruim}")

    # 2.5 Bairro informado: normalização (o campo vem muito sujo: "TORRE 1", "BL A"...)
    df["bairro_informado"] = df["bairro"].map(limpar_bairro)
    df["logradouro_limpo"] = df["logradouro"].map(limpar_logradouro)

    # 2.6 Flags de qualidade para métricas
    df["valor_valido"] = (df["valor_transacao"] >= 10_000).astype(int)
    df["area_construida_m2"] = df["area_construida_m2"].where(df["area_construida_m2"] > 0)
    preco = df["valor_transacao"] / df["area_construida_m2"]
    df["elegivel_m2"] = (
        df["natureza_transacao"].astype(str).str.startswith("1.")
        & (df["proporcao_transmitida_pct"] == 100)
        & df["uso_iptu"].isin(USOS_RESIDENCIAIS)
        & (df["valor_valido"] == 1)
        & df["area_construida_m2"].between(15, 2000)
        & preco.between(500, 50_000)
    ).astype(int)
    df["preco_m2"] = preco.where(df["elegivel_m2"] == 1).round(2)
    log(f"[limpeza] transações com valor < R$ 10 mil (valor_valido=0): {(df['valor_valido'] == 0).sum()}")
    log(f"[limpeza] transações elegíveis para preço do m²: {df['elegivel_m2'].sum()}")
    log(f"[limpeza] linhas finais: {len(df)} (de {n0})")
    return df


# ---------------------------------------------------------------------------
# 3. Modelagem: fato + dimensões
# ---------------------------------------------------------------------------
def modelar(df: pd.DataFrame, usos: pd.DataFrame, padroes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    # dim_tempo (mês de pagamento do ITBI = mês de referência da aba)
    tempo = df[["ano_ref", "mes_ref"]].drop_duplicates().sort_values(["ano_ref", "mes_ref"])
    tempo = tempo.rename(columns={"ano_ref": "ano", "mes_ref": "mes"})
    tempo["mes_id"] = tempo["ano"] * 100 + tempo["mes"]
    tempo["ano_mes"] = tempo["ano"].astype(str) + "-" + tempo["mes"].astype(str).str.zfill(2)
    tempo["nome_mes"] = tempo["mes"].map(NOME_MES)
    tempo["trimestre"] = (tempo["mes"] - 1) // 3 + 1
    tempo["semestre"] = (tempo["mes"] - 1) // 6 + 1
    dim_tempo = tempo[["mes_id", "ano_mes", "ano", "mes", "nome_mes", "trimestre", "semestre"]]

    # dim_natureza
    nat = df["natureza_transacao"].astype(str).drop_duplicates()
    dim_natureza = pd.DataFrame({
        "natureza_id": nat.str.extract(r"^(\d+)\.")[0].astype(int),
        "descricao": nat.str.replace(r"^\d+\.", "", regex=True).str.strip(),
    }).drop_duplicates("natureza_id")
    dim_natureza["grupo"] = dim_natureza["descricao"].map(grupo_natureza)
    dim_natureza = dim_natureza.sort_values("natureza_id")

    # dim_financiamento (0 = sem financiamento declarado)
    fin = df["tipo_financiamento"].dropna().astype(str)
    fin = fin[fin.str.match(r"^\d+\.")].drop_duplicates()
    dim_fin = pd.DataFrame({
        "financiamento_id": fin.str.extract(r"^(\d+)\.")[0].astype(int),
        "descricao": fin.str.replace(r"^\d+\.", "", regex=True).str.strip(),
    }).drop_duplicates("financiamento_id")
    dim_fin = pd.concat([pd.DataFrame({"financiamento_id": [0], "descricao": ["Sem financiamento"]}),
                         dim_fin]).sort_values("financiamento_id")
    sem_desc = dim_fin["descricao"].fillna("").str.strip() == ""
    dim_fin.loc[sem_desc, "descricao"] = ("Código " + dim_fin.loc[sem_desc, "financiamento_id"].astype(str)
                                          + " (sem descrição na fonte)")
    dim_fin["financiado"] = (dim_fin["financiamento_id"] != 0).astype(int)

    # dim_uso e dim_padrao (tabelas oficiais da própria planilha)
    usos = usos.copy()
    usos["uso_id"] = usos["uso_id"].astype(int)
    usos["descricao"] = usos["descricao"].str.strip()
    usos["categoria"] = usos["uso_id"].map(categoria_uso)
    padroes = padroes.copy()
    padroes["padrao_id"] = padroes["padrao_id"].astype(int)
    padroes["tipo"] = padroes["descricao"].str.strip()
    dim_padrao = padroes[["padrao_id", "tipo"]]

    # dim_local: setor fiscal (3 primeiros dígitos do SQL) com bairro e zona de referência
    loc = df.groupby("setor_fiscal").agg(
        bairro=("bairro_informado", lambda s: s.mode().iat[0] if s.notna().any() else None),
        cep_mod=("cep", lambda s: s.mode().iat[0] if s.notna().any() else None),
    ).reset_index()
    loc["bairro"] = loc["bairro"].fillna("SETOR " + loc["setor_fiscal"])
    loc["zona"] = loc["cep_mod"].map(zona_por_cep)
    dim_local = loc[["setor_fiscal", "bairro", "zona"]]

    # Dimensão estável: o nome de bairro de cada setor fica congelado em data/reference/dim_local.csv.
    # Sem isso, carregar mais anos mudaria o "bairro mais frequente" de alguns setores e as mesmas
    # perguntas passariam a ter respostas diferentes. Setores novos recebem o nome calculado acima.
    if REF_LOCAL.exists():
        ref = pd.read_csv(REF_LOCAL, dtype=str)
        novos = dim_local[~dim_local["setor_fiscal"].isin(ref["setor_fiscal"])]
        dim_local = pd.concat([ref[ref["setor_fiscal"].isin(dim_local["setor_fiscal"])], novos],
                              ignore_index=True)
        log(f"[modelagem] dim_local: {len(dim_local) - len(novos)} setores da referência, {len(novos)} novos")
    else:
        REF_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        dim_local.to_csv(REF_LOCAL, index=False)
        log(f"[modelagem] referência de bairros criada em {REF_LOCAL.relative_to(ROOT)}")

    # fato
    fato = pd.DataFrame({
        "transacao_id": range(1, len(df) + 1),
        "mes_id": (df["ano_ref"] * 100 + df["mes_ref"]).values,
        "setor_fiscal": df["setor_fiscal"].values,
        "natureza_id": df["natureza_transacao"].astype(str).str.extract(r"^(\d+)\.")[0].astype(int).values,
        "financiamento_id": (df["tipo_financiamento"].astype(str).str.extract(r"^(\d+)\.")[0]
                             .fillna(0).astype(int).values),
        "uso_id": df["uso_iptu"].fillna(0).astype(int).values,
        "padrao_id": df["padrao_iptu"].fillna(0).astype(int).values,
        "sql_cadastro": df["sql_cadastro"].values,
        "cep": df["cep"].astype("Int64").astype(str).str.zfill(8).replace("0000<NA>", None).values,
        "bairro_informado": df["bairro_informado"].values,
        "logradouro": df["logradouro_limpo"].values,
        "data_transacao": df["data_transacao"].dt.strftime("%Y-%m-%d").values,
        "valor_transacao": df["valor_transacao"].round(2).values,
        "valor_venal_referencia": df["valor_venal_referencia"].values,
        "proporcao_transmitida_pct": df["proporcao_transmitida_pct"].values,
        "valor_venal_ref_proporcional": df["valor_venal_ref_proporcional"].round(2).values,
        "base_calculo": df["base_calculo"].round(2).values,
        "valor_financiado": df["valor_financiado"].round(2).values,
        "area_terreno_m2": df["area_terreno_m2"].values,
        "area_construida_m2": df["area_construida_m2"].values,
        "fracao_ideal": df["fracao_ideal"].values,
        "ano_construcao": df["acc_iptu"].astype("Int64").values,
        "situacao_sql": df["situacao_sql"].values,
        "cartorio": df["cartorio"].astype(str).str.strip().values,
        "valor_valido": df["valor_valido"].values,
        "elegivel_m2": df["elegivel_m2"].values,
        "preco_m2": df["preco_m2"].values,
    })
    # códigos de uso/padrão que não existem nas tabelas oficiais -> 0 (terreno/indefinido)
    fato.loc[~fato["uso_id"].isin(usos["uso_id"]), "uso_id"] = 0
    fato.loc[~fato["padrao_id"].isin(dim_padrao["padrao_id"]), "padrao_id"] = 0

    return {
        "fato_transacoes": fato,
        "dim_tempo": dim_tempo,
        "dim_local": dim_local,
        "dim_natureza": dim_natureza,
        "dim_financiamento": dim_fin,
        "dim_uso": usos[["uso_id", "descricao", "categoria"]],
        "dim_padrao": dim_padrao,
    }


# ---------------------------------------------------------------------------
# 4. Gravação no SQLite
# ---------------------------------------------------------------------------
DDL = """
CREATE TABLE dim_tempo (mes_id INTEGER PRIMARY KEY, ano_mes TEXT, ano INTEGER, mes INTEGER,
                        nome_mes TEXT, trimestre INTEGER, semestre INTEGER);
CREATE TABLE dim_local (setor_fiscal TEXT PRIMARY KEY, bairro TEXT, zona TEXT);
CREATE TABLE dim_natureza (natureza_id INTEGER PRIMARY KEY, descricao TEXT, grupo TEXT);
CREATE TABLE dim_financiamento (financiamento_id INTEGER PRIMARY KEY, descricao TEXT, financiado INTEGER);
CREATE TABLE dim_uso (uso_id INTEGER PRIMARY KEY, descricao TEXT, categoria TEXT);
CREATE TABLE dim_padrao (padrao_id INTEGER PRIMARY KEY, tipo TEXT);
CREATE TABLE fato_transacoes (
    transacao_id INTEGER PRIMARY KEY,
    mes_id INTEGER REFERENCES dim_tempo(mes_id),
    setor_fiscal TEXT REFERENCES dim_local(setor_fiscal),
    natureza_id INTEGER REFERENCES dim_natureza(natureza_id),
    financiamento_id INTEGER REFERENCES dim_financiamento(financiamento_id),
    uso_id INTEGER REFERENCES dim_uso(uso_id),
    padrao_id INTEGER REFERENCES dim_padrao(padrao_id),
    sql_cadastro TEXT, cep TEXT, bairro_informado TEXT, logradouro TEXT, data_transacao TEXT,
    valor_transacao REAL, valor_venal_referencia REAL, proporcao_transmitida_pct REAL,
    valor_venal_ref_proporcional REAL, base_calculo REAL, valor_financiado REAL,
    area_terreno_m2 REAL, area_construida_m2 REAL, fracao_ideal REAL, ano_construcao INTEGER,
    situacao_sql TEXT, cartorio TEXT, valor_valido INTEGER, elegivel_m2 INTEGER, preco_m2 REAL
);
CREATE INDEX ix_fato_mes ON fato_transacoes(mes_id);
CREATE INDEX ix_fato_setor ON fato_transacoes(setor_fiscal);
CREATE INDEX ix_fato_uso ON fato_transacoes(uso_id);
CREATE INDEX ix_fato_nat ON fato_transacoes(natureza_id);
CREATE INDEX ix_fato_logr ON fato_transacoes(logradouro);
"""


def gravar(tabelas: dict[str, pd.DataFrame]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript(DDL)
    for nome in ["dim_tempo", "dim_local", "dim_natureza", "dim_financiamento", "dim_uso",
                 "dim_padrao", "fato_transacoes"]:
        tabelas[nome].to_sql(nome, con, if_exists="append", index=False)
    con.commit()
    con.execute("VACUUM")
    con.close()
    log(f"Banco gravado em {DB_PATH.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# 5. Testes de qualidade
# ---------------------------------------------------------------------------
def testes_qualidade() -> bool:
    con = sqlite3.connect(DB_PATH)
    q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
    testes = []

    # volume mínimo exigido pelo projeto
    n = q("SELECT COUNT(*) FROM fato_transacoes")
    testes.append(("fato com >= 5.000 linhas", n >= 5000, n))
    ncols = len(con.execute("PRAGMA table_info(fato_transacoes)").fetchall())
    testes.append(("fato com >= 8 colunas", ncols >= 8, ncols))

    # nulos em colunas obrigatórias
    for col in ["mes_id", "setor_fiscal", "natureza_id", "uso_id", "padrao_id", "valor_transacao"]:
        v = q(f"SELECT COUNT(*) FROM fato_transacoes WHERE {col} IS NULL")
        testes.append((f"sem nulos em fato.{col}", v == 0, v))

    # duplicatas de chave
    v = q("SELECT COUNT(*) - COUNT(DISTINCT transacao_id) FROM fato_transacoes")
    testes.append(("transacao_id único", v == 0, v))
    for dim, pk in [("dim_tempo", "mes_id"), ("dim_local", "setor_fiscal"), ("dim_natureza", "natureza_id"),
                    ("dim_financiamento", "financiamento_id"), ("dim_uso", "uso_id"), ("dim_padrao", "padrao_id")]:
        v = q(f"SELECT COUNT(*) - COUNT(DISTINCT {pk}) FROM {dim}")
        testes.append((f"{dim}.{pk} único", v == 0, v))

    # chaves órfãs (FK da fato sem correspondente na dimensão)
    for dim, pk in [("dim_tempo", "mes_id"), ("dim_local", "setor_fiscal"), ("dim_natureza", "natureza_id"),
                    ("dim_financiamento", "financiamento_id"), ("dim_uso", "uso_id"), ("dim_padrao", "padrao_id")]:
        v = q(f"SELECT COUNT(*) FROM fato_transacoes f LEFT JOIN {dim} d ON f.{pk} = d.{pk} WHERE d.{pk} IS NULL")
        testes.append((f"sem órfãos fato -> {dim}", v == 0, v))

    # regras de domínio
    v = q("SELECT COUNT(*) FROM fato_transacoes WHERE valor_transacao <= 0")
    testes.append(("valor_transacao > 0", v == 0, v))
    v = q("SELECT COUNT(*) FROM fato_transacoes WHERE base_calculo + 0.01 < valor_transacao")
    testes.append(("base_calculo >= valor_transacao (regra do ITBI)", v / n < 0.01, v))
    v = q("SELECT COUNT(*) FROM fato_transacoes WHERE elegivel_m2 = 1 AND preco_m2 IS NULL")
    testes.append(("preco_m2 preenchido quando elegível", v == 0, v))
    v = q("SELECT COUNT(DISTINCT mes_id) FROM fato_transacoes")
    anos = q("SELECT COUNT(DISTINCT ano) FROM dim_tempo")
    testes.append((f"12 meses para cada um dos {anos} anos carregados", v == 12 * anos, v))

    log("\n=== TESTES DE QUALIDADE ===")
    todos_ok = True
    for nome, ok, valor in testes:
        todos_ok &= bool(ok)
        log(f"[{'OK ' if ok else 'FALHOU'}] {nome}  (valor: {valor})")
    log("\n=== RESUMO DAS TABELAS ===")
    for t in ["fato_transacoes", "dim_tempo", "dim_local", "dim_natureza", "dim_financiamento", "dim_uso", "dim_padrao"]:
        log(f"{t:20s} {q(f'SELECT COUNT(*) FROM {t}'):>8} linhas")
    con.close()
    return todos_ok


def main() -> None:
    bruto, usos, padroes = ler_planilhas()
    df = limpar(bruto)
    tabelas = modelar(df, usos, padroes)
    gravar(tabelas)
    ok = testes_qualidade()
    REPORT_PATH.write_text("\n".join(log_lines), encoding="utf-8")
    log(f"\nRelatório salvo em {REPORT_PATH.relative_to(ROOT)}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
