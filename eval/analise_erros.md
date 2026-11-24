# Análise de erros — golden set

Modelo: `openai/gpt-oss-120b` via Groq (plano gratuito), `reasoning_effort = low`.
Detalhe pergunta a pergunta em `eval/resultados.csv`; tabela completa em `eval/resumo_metricas.md`.

## 1. Métricas antes e depois

| Rodada | Acurácia | SQL válido na 1ª | Média de tentativas de correção | Tempo médio (s)* | Tokens médios |
|---|---|---|---|---|---|
| antes | 65,0% (13/20) | 88,2% | 0,12 | 39,0 | 6.324 |
| **depois** | **90,0% (18/20)** | **94,4%** | **0,06** | 45,9 | 7.325 |

| Nível | Antes | Depois |
|---|---|---|
| Fácil | 85,7% | **100%** |
| Médio | 28,6% | **100%** |
| Difícil | 75,0% | 50,0% |
| Armadilha | 100% | 100% |

As 7 falhas da rodada "antes" foram todas resolvidas. Surgiram 2 falhas novas em perguntas difíceis
(seção 4). O custo das correções foi ~1.000 tokens a mais por pergunta (+16%).

\* O tempo inclui as esperas do limite de **tokens por minuto** do plano grátis do Groq (8 mil TPM).
Sem a espera, cada pergunta leva ~3 s (medido na primeira pergunta).

## 2. Falhas da rodada "antes"

O SQL rodou sem erro em 88% das perguntas: as falhas não são de sintaxe, são de **significado**
(o SQL executa, mas filtra ou agrupa de forma errada).

| ID | Pergunta | O que o sistema fez | Causa | Correção aplicada |
|---|---|---|---|---|
| F06 | Imóveis arrematados em leilão (2024+2025) | `n.descricao = 'LEILÃO'` → 0 linhas casaram; respondeu "nenhum leilão" | **Alucinação de valor** de filtro (o texto real é "Arrematação (em leilão ou hasta pública)") | Sinônimos de valores (leilão → `f.natureza_id = 4`) passados ao Agente SQL + regra "nunca invente valores de texto" + guardrail de resultado vazio |
| M02 | Preço do m² de apartamentos em Pinheiros 2025 | `u.descricao = 'APARTAMENTO'` → NULL | **Alucinação de valor** (a coluna certa é `u.categoria`) | Sinônimo apartamento → `u.categoria = 'Residencial vertical (apartamento/flat)'` + guardrail de resultado vazio |
| M04 | Volume financeiro mês a mês em 2025 | Agrupou por `mes_id` (202501…) em vez do mês (1…12). Valores corretos | **Formato do resultado** (rótulo de mês) | Regra: séries mensais de um ano devolvem `t.mes` |
| M05 | % das vendas de 2025 com MCMV | `fi.descricao = 'MINHA CASA MINHA VIDA'` (maiúsculas) → 0,0% | **Alucinação de valor** (grafia: "Minha Casa Minha Vida") | Sinônimo MCMV → `f.financiamento_id = 2` |
| M06 | Valor médio de casas x apartamentos 2024 | `u.descricao IN ('CASA','APARTAMENTO')` → 0 linhas | **Alucinação de valor** | Sinônimos casa/apartamento → `u.categoria` + guardrail de resultado vazio |
| M07 | Vagas de garagem negociadas no 1º semestre 2025 | Classificou como **fora de escopo** | **Contexto faltando** no Orquestrador: o resumo do schema não mostrava que "garagem" é um uso de imóvel | Sinônimos de valores no `get_schema()` + regra "garagens, lojas, terrenos… estão no escopo" |
| D03 | Meses de 2025 com mais vendas que o mesmo mês de 2024 | Cruzou os anos por `substr(mes_id)` e devolveu `mes_id` (202501). Meses e contagens corretos | **Formato do resultado** (rótulo de mês) | Regra: comparação do mesmo mês entre anos agrupa por `t.mes` |

### Causa raiz das alucinações (5 de 7 falhas)

Na primeira versão, a camada semântica era enviada inteira ao modelo. Para caber no limite gratuito
do Groq (8 mil tokens por minuto), o contexto foi **enxugado** e os **sinônimos de valores** deixaram
de ir para o Agente SQL. Sem eles, o modelo "chutou" textos plausíveis (`'APARTAMENTO'`, `'LEILÃO'`)
que não existem nas dimensões. Isso mostra na prática por que a camada semântica é a parte central:
sem o "conhecimento tácito" (qual coluna e qual valor exato usar), o SQL roda mas responde errado.

### Por que um resultado vazio não foi percebido

O SQL vazio/NULL é "válido", então o agente aceitava e o Analista explicava o vazio
("não houve leilões"). É o erro mais perigoso: a resposta parece confiável e está errada.

## 3. Correções aplicadas (todas genéricas, nenhuma escreve a resposta de uma pergunta do golden set)

- `src/semantic_layer.yaml`: 3 regras novas — não inventar valores de texto (usar códigos/sinônimos);
  garagens, lojas, terrenos etc. estão no escopo; séries mensais usam `t.mes`.
- `src/tools.py`: `get_schema()` (Orquestrador) passa a incluir os **sinônimos de valores**.
- `src/agents.py`: o contexto do Agente SQL inclui os sinônimos de valores; **novo guardrail**: se a
  consulta voltar vazia ou só com NULL, o agente recebe um aviso e tem mais uma tentativa para revisar
  os filtros (se repetir o mesmo SQL, o vazio é aceito como resposta).

Custo das correções: ~500 tokens a mais por chamada.

## 4. Rodada "depois": o que mudou

Todas as 7 falhas da rodada "antes" passaram a acertar, com o filtro certo: `f.natureza_id = 4` (F06),
`u.categoria = 'Residencial vertical (apartamento/flat)'` (M02), `t.mes` (M04, D03), `f.financiamento_id = 2` (M05),
`u.categoria IN (...)` (M06) e "vagas de garagem" classificada como pergunta de dados (M07).

### Falhas novas (regressões em perguntas que acertaram no "antes")

| ID | Pergunta | O que o sistema fez | A resposta ao usuário estava certa? | Causa |
|---|---|---|---|---|
| D02 | Crescimento % do volume de cada zona 2024→2025 | Devolveu só `zona` + `variacao_pct`, com os valores exatos do gabarito (Oeste 17,9%, Leste 14,0%, Centro 5,8%, Norte 5,0%, Sul 0,2%), sem as colunas de volume de cada ano | **Sim** | **Critério de avaliação rígido**: o SQL de referência traz os volumes intermediários e o avaliador exige todas as colunas da referência |
| D04 | Entre os 10 bairros de maior volume, qual tem o maior % de vendas financiadas? | O SQL parou no top 10 (10 linhas) e não escolheu o maior; o Analista leu as 10 linhas e respondeu certo: Barra Funda, 72,0% | **Sim** (no texto), não no SQL | **SQL incompleto**: a segunda etapa da pergunta ficou para o Analista |

**Por que pioraram, se a camada semântica só ganhou informação?** A LLM não é determinística: com o mesmo prompt,
pode montar o SQL de outro jeito. As duas foram perguntas de duas etapas (calcular por grupo e depois comparar),
onde há mais de um formato de resposta razoável.

**O que faríamos em seguida** (não aplicado, para não ajustar o sistema às perguntas do golden set):
- Regra genérica: "quando a pergunta pede UM item ('qual tem o maior…'), o SQL deve terminar com `ORDER BY … LIMIT 1`".
- No avaliador: aceitar resultado que contenha as colunas que a pergunta pede, mesmo sem as intermediárias. Por essa
  régua, o D02 seria acerto e a acurácia "depois" seria 95% (19/20). Mantivemos a nota oficial em 90% porque o critério
  foi definido antes da rodada.
- Rodar cada pergunta mais de uma vez e medir a estabilidade, já que a mesma pergunta pode acertar numa rodada e errar
  na outra.

## 5. Outras decisões tomadas por teste

| Teste | Resultado | Decisão |
|---|---|---|
| Gemini `gemini-4-hydrogen` | Erro 400 (thought signature) → corrigido no código; depois erro 503 (alta demanda) | Descartado |
| Gemini `gemini-3.6-flash` | Acertou, mas 90–176 s por pergunta e limite de 20 requisições/dia no plano grátis (~4 perguntas) | Descartado |
| Gemini `gemini-3.5-flash-lite` | 6 s, mas não chamava as ferramentas (escrevia o SQL no texto) | Descartado; adicionamos um "plano B" que executa SQL escrito no texto |
| Groq `openai/gpt-oss-120b` | ~3 s por pergunta, grátis, 1.000 req/dia | **Escolhido** |
