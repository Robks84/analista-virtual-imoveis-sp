# Dados originais

Fonte: Prefeitura de São Paulo — Secretaria Municipal da Fazenda
"Dados das Transações Imobiliárias com recolhimento de ITBI"
https://prefeitura.sp.gov.br/fazenda/w/acesso_a_informacao/31501

Arquivos usados (formato Excel/xlsx): um por ano, de 2019 a 2025, cada um com 12 abas mensais
(ex.: `GUIAS-DE-ITBI-PAGAS-2024.xlsx`; o de 2025 se chama `GUIAS_DE_ITBI_PAGAS__28012026__XLS.xlsx`).
O código lê todos os `.xlsx` desta pasta; dá para usar só alguns anos.

Cada linha = uma Declaração de Transação Imobiliária (DTI) com ITBI pago no mês da aba.
As abas LEGENDA, EXPLICAÇÕES, Tabela de USOS e Tabela de PADRÕES documentam os campos.

Se os arquivos forem grandes demais para o repositório, baixe-os no link acima e coloque-os nesta pasta
antes de rodar `python src/load_data.py`.
