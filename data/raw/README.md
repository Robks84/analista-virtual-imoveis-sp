# Dados originais

Fonte: Prefeitura de São Paulo — Secretaria Municipal da Fazenda
"Dados das Transações Imobiliárias com recolhimento de ITBI"
https://prefeitura.sp.gov.br/fazenda/w/acesso_a_informacao/31501

Arquivos usados (formato Excel/xlsx):
- `GUIAS-DE-ITBI-PAGAS-2024.xlsx` (12 abas mensais, JAN-2024 a DEZ-2024)
- `GUIAS_DE_ITBI_PAGAS__28012026__XLS.xlsx` (12 abas mensais, JAN-2025 a DEZ-2025)

Cada linha = uma Declaração de Transação Imobiliária (DTI) com ITBI pago no mês da aba.
As abas LEGENDA, EXPLICAÇÕES, Tabela de USOS e Tabela de PADRÕES documentam os campos.

Se os arquivos forem grandes demais para o repositório, baixe-os no link acima e coloque-os nesta pasta
antes de rodar `python src/load_data.py`.
