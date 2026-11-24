# Uso de IA no desenvolvimento

> Documento obrigatório (seção 7 do enunciado). **Revisem e completem com o que o grupo fez de fato**,
> principalmente a seção 3, antes da entrega.

## 1. Ferramentas usadas

| Ferramenta | Para quê |
|---|---|
| Claude (Anthropic), via claude.ai | Exploração inicial das planilhas, código de carga, rascunho da camada semântica, rascunho do golden set, código dos agentes, testes e documentação |
| LLM do sistema (`LLM_MODEL` no `.env`) | Executa os 3 agentes em tempo de uso (não é ferramenta de desenvolvimento) |
| _(completar: ChatGPT, Copilot, etc., se usaram)_ | |

## 2. Partes geradas ou assistidas por IA

| Arquivo | Como a IA ajudou | Revisão feita pelo grupo |
|---|---|---|
| `src/load_data.py` | Identificou que as abas JAN-2024 e OUT-2024 vêm sem cabeçalho e que outras têm cabeçalhos trocados; propôs leitura posicional, limpeza do bairro e uso do setor fiscal | _(ex.: conferimos contagens por mês com a planilha original)_ |
| `src/semantic_layer.yaml` | Rascunho de descrições, métricas, sinônimos e regras de negócio | _(ex.: ajustamos sinônimos e regras)_ |
| `eval/golden_set.csv` | Rascunho das 20 perguntas e SQLs de referência | _(ex.: cada SQL foi conferido manualmente por ___ )_ |
| `src/tools.py`, `src/agents.py`, `src/orchestrator.py`, `src/app.py` | Código inicial dos agentes, guardrails e interface | |
| `eval/run_eval.py`, `tests/` | Script de avaliação e testes dos guardrails | |
| `README.md` | Estrutura e diagramas | |

## 3. O que precisou ser corrigido manualmente

_(Preencher com exemplos reais. Sugestões do que observar:)_

- Erros encontrados ao rodar com o modelo real (ex.: formato de JSON, nomes de bairro com acento).
- SQLs de referência do golden set que o grupo discordou e reescreveu.
- Regras de negócio que a IA não sabia (ex.: decisões sobre o que conta como "venda").
- Ajustes de prompt feitos após a rodada "antes" (registrados em `eval/analise_erros.md`).

## 4. Decisões tomadas pelo grupo (não delegadas à IA)

- Escolha do tema e da base (ITBI/Prefeitura de SP, 2024–2025).
- Definição das métricas oficiais e das regras de negócio.
- Validação das respostas esperadas do golden set.
