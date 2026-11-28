"""
Gera eval/golden_set.csv a partir das perguntas + SQL de referência escritos À MÃO abaixo.
A resposta esperada é calculada executando o SQL de referência no banco.

As perguntas foram definidas ANTES de escrever/ajustar os prompts dos agentes.
Não altere as perguntas depois de começar a ajustar os prompts (evita overfitting).

Uso: python eval/build_golden_set.py
"""
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "processed" / "itbi.db"
OUT = ROOT / "eval" / "golden_set.csv"

J_T = "JOIN dim_tempo t ON f.mes_id = t.mes_id"
J_L = "JOIN dim_local l ON f.setor_fiscal = l.setor_fiscal"
J_U = "JOIN dim_uso u ON f.uso_id = u.uso_id"

GOLDEN = [
    # ------------------------------- FÁCIL (7) -------------------------------
    ("F01", "facil", "dados", "Quantas transações com ITBI pago tivemos em 2024?",
     f"SELECT COUNT(*) AS qtd_transacoes FROM fato_transacoes f {J_T} WHERE t.ano = 2024"),
    ("F02", "facil", "dados", "Quantos imóveis foram vendidos em 2025?",
     f"SELECT COUNT(*) AS qtd_vendas FROM fato_transacoes f {J_T} WHERE t.ano = 2025 AND f.natureza_id = 1"),
    ("F03", "facil", "dados", "Qual foi o valor médio das transações em 2025?",
     f"SELECT ROUND(AVG(f.valor_transacao), 2) AS valor_medio FROM fato_transacoes f {J_T} "
     "WHERE t.ano = 2025 AND f.valor_valido = 1"),
    ("F04", "facil", "dados", "Qual foi o volume financeiro total negociado em 2024?",
     f"SELECT ROUND(SUM(f.valor_transacao), 2) AS volume_financeiro FROM fato_transacoes f {J_T} "
     "WHERE t.ano = 2024 AND f.valor_valido = 1"),
    ("F05", "facil", "dados", "Qual o preço médio do metro quadrado em 2025?",
     f"SELECT ROUND(AVG(f.preco_m2), 2) AS preco_medio_m2 FROM fato_transacoes f {J_T} "
     "WHERE t.ano = 2025 AND f.elegivel_m2 = 1"),
    ("F06", "facil", "dados", "Quantos imóveis foram arrematados em leilão em 2024 e 2025 somados?",
     f"SELECT COUNT(*) AS qtd_leiloes FROM fato_transacoes f {J_T} WHERE t.ano IN (2024, 2025) AND f.natureza_id = 4"),
    ("F07", "facil", "dados", "Quanto foi financiado no total (crédito imobiliário) em 2025?",
     f"SELECT ROUND(SUM(f.valor_financiado), 2) AS valor_financiado_total FROM fato_transacoes f {J_T} "
     "WHERE t.ano = 2025 AND f.financiamento_id <> 0"),

    # ------------------------------- MÉDIO (7) -------------------------------
    ("M01", "medio", "dados", "Quantas vendas tivemos por zona no último trimestre?",
     f"SELECT l.zona, COUNT(*) AS qtd_vendas FROM fato_transacoes f {J_T} {J_L} "
     "WHERE t.ano = 2025 AND t.trimestre = 4 AND f.natureza_id = 1 GROUP BY l.zona ORDER BY qtd_vendas DESC"),
    ("M02", "medio", "dados", "Qual o preço médio do m² de apartamentos em Pinheiros em 2025?",
     f"SELECT ROUND(AVG(f.preco_m2), 2) AS preco_medio_m2 FROM fato_transacoes f {J_T} {J_L} {J_U} "
     "WHERE t.ano = 2025 AND f.elegivel_m2 = 1 AND l.bairro = 'PINHEIROS' "
     "AND u.categoria = 'Residencial vertical (apartamento/flat)'"),
    ("M03", "medio", "dados", "Quais os 5 bairros com mais vendas em 2025?",
     f"SELECT l.bairro, COUNT(*) AS qtd_vendas FROM fato_transacoes f {J_T} {J_L} "
     "WHERE t.ano = 2025 AND f.natureza_id = 1 GROUP BY l.bairro ORDER BY qtd_vendas DESC LIMIT 5"),
    ("M04", "medio", "dados", "Mostre o volume financeiro mês a mês em 2025.",
     f"SELECT t.mes, ROUND(SUM(f.valor_transacao), 2) AS volume_financeiro FROM fato_transacoes f {J_T} "
     "WHERE t.ano = 2025 AND f.valor_valido = 1 GROUP BY t.mes ORDER BY t.mes"),
    ("M05", "medio", "dados", "Qual o percentual das vendas de 2025 que foram financiadas pelo Minha Casa Minha Vida?",
     f"SELECT ROUND(100.0 * SUM(CASE WHEN f.financiamento_id = 2 THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_mcmv "
     f"FROM fato_transacoes f {J_T} WHERE t.ano = 2025 AND f.natureza_id = 1"),
    ("M06", "medio", "dados", "Compare o valor médio de venda de casas e de apartamentos em 2024.",
     f"SELECT u.categoria, ROUND(AVG(f.valor_transacao), 2) AS valor_medio FROM fato_transacoes f {J_T} {J_U} "
     "WHERE t.ano = 2024 AND f.natureza_id = 1 AND f.valor_valido = 1 "
     "AND u.categoria IN ('Residencial horizontal (casa)', 'Residencial vertical (apartamento/flat)') "
     "GROUP BY u.categoria"),
    ("M07", "medio", "dados", "Quantas vagas de garagem foram negociadas no primeiro semestre de 2025?",
     f"SELECT COUNT(*) AS qtd_transacoes FROM fato_transacoes f {J_T} {J_U} "
     "WHERE t.ano = 2025 AND t.semestre = 1 AND u.categoria = 'Garagem'"),

    # ------------------------------- DIFÍCIL (4) -----------------------------
    ("D01", "dificil", "dados",
     "Quais os 5 bairros que mais valorizaram em % no preço do m² de 2024 para 2025?",
     f"""WITH p AS (
  SELECT l.bairro,
         AVG(CASE WHEN t.ano = 2024 THEN f.preco_m2 END) AS m2_2024,
         AVG(CASE WHEN t.ano = 2025 THEN f.preco_m2 END) AS m2_2025,
         SUM(CASE WHEN t.ano = 2024 THEN 1 ELSE 0 END) AS n_2024,
         SUM(CASE WHEN t.ano = 2025 THEN 1 ELSE 0 END) AS n_2025
  FROM fato_transacoes f {J_T} {J_L}
  WHERE f.elegivel_m2 = 1
  GROUP BY l.bairro)
SELECT bairro, ROUND(m2_2024, 2) AS m2_2024, ROUND(m2_2025, 2) AS m2_2025,
       ROUND(100.0 * (m2_2025 - m2_2024) / m2_2024, 1) AS variacao_pct
FROM p WHERE n_2024 >= 100 AND n_2025 >= 100
ORDER BY variacao_pct DESC LIMIT 5"""),
    ("D02", "dificil", "dados",
     "Qual foi o crescimento percentual do volume financeiro de cada zona entre 2024 e 2025?",
     f"""SELECT l.zona,
       ROUND(SUM(CASE WHEN t.ano = 2024 THEN f.valor_transacao ELSE 0 END), 2) AS volume_2024,
       ROUND(SUM(CASE WHEN t.ano = 2025 THEN f.valor_transacao ELSE 0 END), 2) AS volume_2025,
       ROUND(100.0 * (SUM(CASE WHEN t.ano = 2025 THEN f.valor_transacao ELSE 0 END)
                    - SUM(CASE WHEN t.ano = 2024 THEN f.valor_transacao ELSE 0 END))
             / SUM(CASE WHEN t.ano = 2024 THEN f.valor_transacao ELSE 0 END), 1) AS crescimento_pct
FROM fato_transacoes f {J_T} {J_L}
WHERE f.valor_valido = 1
GROUP BY l.zona ORDER BY crescimento_pct DESC"""),
    ("D03", "dificil", "dados",
     "Em quais meses de 2025 o número de vendas foi maior que no mesmo mês de 2024?",
     f"""SELECT t.mes,
       SUM(CASE WHEN t.ano = 2024 THEN 1 ELSE 0 END) AS vendas_2024,
       SUM(CASE WHEN t.ano = 2025 THEN 1 ELSE 0 END) AS vendas_2025
FROM fato_transacoes f {J_T}
WHERE f.natureza_id = 1
GROUP BY t.mes
HAVING SUM(CASE WHEN t.ano = 2025 THEN 1 ELSE 0 END) > SUM(CASE WHEN t.ano = 2024 THEN 1 ELSE 0 END)
ORDER BY t.mes"""),
    ("D04", "dificil", "dados",
     "Entre os 10 bairros com maior volume financeiro em 2025, qual tem o maior percentual de vendas financiadas?",
     f"""WITH top10 AS (
  SELECT l.bairro FROM fato_transacoes f {J_T} {J_L}
  WHERE t.ano = 2025 AND f.valor_valido = 1
  GROUP BY l.bairro ORDER BY SUM(f.valor_transacao) DESC LIMIT 10)
SELECT l.bairro,
       ROUND(100.0 * SUM(CASE WHEN f.financiamento_id <> 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_financiadas
FROM fato_transacoes f {J_T} {J_L}
WHERE t.ano = 2025 AND f.natureza_id = 1 AND l.bairro IN (SELECT bairro FROM top10)
GROUP BY l.bairro ORDER BY pct_financiadas DESC LIMIT 1"""),

    # ------------------------------ ARMADILHA (2) ----------------------------
    ("A01", "armadilha", "fora_escopo", "Qual vai ser o preço do m² em Moema em 2027?", ""),
    ("A02", "armadilha", "ambigua", "Quais são os melhores bairros?", ""),
]


def resumir(cols, rows) -> str:
    if not rows:
        return "(sem linhas)"
    if len(rows) == 1 and len(cols) == 1:
        v = rows[0][0]
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if isinstance(v, float) else str(v)
    linhas = ["; ".join(f"{c}={v}" for c, v in zip(cols, r)) for r in rows[:12]]
    return " | ".join(linhas)


def main():
    con = sqlite3.connect(DB)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "nivel", "tipo_esperado", "pergunta", "sql_referencia", "resposta_esperada"])
        for gid, nivel, tipo, pergunta, sql in GOLDEN:
            if sql:
                cur = con.execute(sql)
                cols = [d[0] for d in cur.description]
                resp = resumir(cols, cur.fetchall())
            else:
                resp = ("Recusar: previsão está fora do escopo (base só tem 2024–2025)" if tipo == "fora_escopo"
                        else "Pedir esclarecimento: 'melhores' em qual critério (preço do m², volume, valorização)?")
            w.writerow([gid, nivel, tipo, pergunta, sql, resp])
            print(f"{gid} [{nivel}] {pergunta}\n   -> {resp[:160]}")
    print(f"\nGolden set salvo em {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
