"""Textos e dados compartilhados pelas interfaces (Gradio e Streamlit)."""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTADOS = ROOT / "eval" / "resultados.csv"

TITULO = "Do BI ao Analista Virtual — Imóveis SP"

SUBTITULO = (
    "Faça perguntas de negócio em português sobre o mercado imobiliário de São Paulo: vendas, "
    "valores, preço do m², financiamento, bairros e zonas. Cada consulta é validada antes de rodar "
    "no banco, e o SQL usado aparece sempre."
)

ESCOPO = """### Escopo da base
- **Fonte:** Prefeitura de SP — transações com ITBI pago (dados abertos)
- **Período:** jan/2024 a dez/2025 (mês de pagamento do ITBI)
- **Registros:** 446.916 transações
- **Moeda:** reais (R$ nominais)
- **Modelo:** esquema estrela (`fato_transacoes` + 6 dimensões)
- **Travas:** somente `SELECT`, `LIMIT` automático, tempo limite de 10 s, sem dados de pessoas
"""

AVISO_MEMORIA = (
    "Cada pergunta é respondida de forma independente. Para continuar uma análise, repita o contexto: "
    "*\"preço do m² em Pinheiros em 2025\"* em vez de *\"e em 2025?\"*."
)

EXEMPLOS = [
    "Quantas vendas de apartamentos tivemos em 2025?",
    "Qual o preço médio do m² por zona em 2025?",
    "Top 10 bairros com maior volume financeiro em 2024",
    "Como evoluiu o número de vendas mês a mês em 2025?",
    "Qual o percentual de vendas financiadas em Moema em 2025?",
    "Quantos imóveis foram arrematados em leilão em 2025?",
    "Qual vai ser o preço do m² em 2030?",
]

COMO_FUNCIONA = """### Três agentes dividem o trabalho

| # | Agente | O que faz | Ferramentas |
|---|---|---|---|
| 1 | **Orquestrador** | Classifica a pergunta (dados, fora de escopo ou ambígua). Recusa com educação ou pede esclarecimento quando precisa. Busca na camada semântica as tabelas, métricas e regras e monta o plano | `get_schema`, `get_metric` |
| 2 | **Agente SQL** | Escreve o SQL a partir do plano, valida, executa e corrige se der erro (até 3 tentativas). Se a consulta volta vazia, revisa os filtros | `validate_sql`, `run_sql` |
| 3 | **Analista** | Transforma o resultado em resposta de negócio: números, destaques, ressalvas e uma próxima pergunta sugerida | — |

### Camada semântica — a "memória do analista"
O arquivo `semantic_layer.yaml` diz ao sistema o que a LLM não consegue adivinhar olhando só as tabelas:
- **métricas oficiais** (ex.: preço do m² = média de `preco_m2` só para vendas residenciais do imóvel inteiro);
- **sinônimos** ("apto" = apartamento, "MCMV" = Minha Casa Minha Vida, "leilão" = arrematação);
- **regras de negócio** ("vendas" = compra e venda; o tempo padrão é o mês de pagamento do ITBI).

### Guardrails (no código, não só no prompt)
- Somente leitura: bloqueia `INSERT`, `UPDATE`, `DELETE`, `DROP`… e a conexão com o banco é read-only
- `LIMIT` automático e timeout de 10 s por consulta
- Perguntas fora do escopo são recusadas; perguntas ambíguas geram uma pergunta de volta
- O SQL usado aparece sempre para o usuário
- Chave da API fora do código (variável de ambiente / Secrets do Colab)

### Stack
Python · SQLite · LLM `openai/gpt-oss-120b` via Groq (API compatível com OpenAI) · Gradio
"""

NOME_NIVEL = {"facil": "Fácil", "medio": "Médio", "dificil": "Difícil", "armadilha": "Armadilha"}
COLUNAS_RESUMO = ["Rodada", "Recorte", "Acertos", "Acurácia", "SQL válido na 1ª", "Tempo médio (s)"]
COLUNAS_DETALHE = ["Rodada", "ID", "Nível", "Pergunta", "Acertou", "Motivo"]


def carregar_avaliacao() -> tuple[list[list], list[list]]:
    """Lê eval/resultados.csv e devolve (resumo por rodada e nível, detalhe por pergunta)."""
    if not RESULTADOS.exists():
        return [], []
    with open(RESULTADOS, encoding="utf-8") as fh:
        linhas = list(csv.DictReader(fh))
    resumo = []
    for rodada in dict.fromkeys(x["rodada"] for x in linhas):
        da_rodada = [x for x in linhas if x["rodada"] == rodada]
        grupos = [("Geral", da_rodada)] + [(NOME_NIVEL[n], [x for x in da_rodada if x["nivel"] == n])
                                           for n in NOME_NIVEL]
        for nome, grupo in grupos:
            if not grupo:
                continue
            acertos = sum(int(x["acertou"]) for x in grupo)
            com_sql = [x for x in grupo if str(x["tentativas_sql"]) not in ("", "0")]
            validos = sum(int(x["sql_valido_1a"] or 0) for x in com_sql)
            resumo.append([
                rodada, nome, f"{acertos}/{len(grupo)}", f"{100 * acertos / len(grupo):.1f}%",
                f"{100 * validos / len(com_sql):.1f}%" if com_sql else "—",
                f"{sum(float(x['tempo_s']) for x in grupo) / len(grupo):.1f}",
            ])
    detalhe = [[x["rodada"], x["id"], NOME_NIVEL.get(x["nivel"], x["nivel"]), x["pergunta"],
                "✔" if int(x["acertou"]) else "✘", x["motivo"]] for x in linhas]
    return resumo, detalhe
