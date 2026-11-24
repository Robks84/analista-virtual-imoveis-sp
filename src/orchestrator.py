"""
Fluxo entre os agentes:

    pergunta
      -> Orquestrador (classifica + busca contexto na camada semântica)
          -> fora_escopo / ambigua: devolve a mensagem ao usuário e para
          -> dados: Agente SQL (gera, valida, executa, corrige até 3x)
              -> Analista (resposta de negócio)
      -> RespostaFinal (texto + tabela + SQL usado + métricas de execução)

Uso rápido pelo terminal:
    python src/orchestrator.py "Quantas vendas tivemos em 2025?"
"""
from __future__ import annotations

import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from agents import LLM, AgenteSQL, Analista, CotaDiariaEsgotada, Orquestrador, Plano


@dataclass
class RespostaFinal:
    pergunta: str
    classificacao: str
    texto: str
    sql: str = ""
    colunas: list[str] = field(default_factory=list)
    linhas: list[list[Any]] = field(default_factory=list)
    suposicoes: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    tentativas_sql: int = 0
    sql_valido_primeira: bool | None = None
    historico_sql: list[dict] = field(default_factory=list)
    tempo_s: float = 0.0
    chamadas_llm: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    erro: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class AnalistaVirtual:
    def __init__(self, llm: LLM | None = None):
        self.llm = llm or LLM()
        self.orquestrador = Orquestrador(self.llm)
        self.agente_sql = AgenteSQL(self.llm)
        self.analista = Analista(self.llm)

    def responder(self, pergunta: str, historico: list[dict] | None = None) -> RespostaFinal:
        inicio = time.time()
        uso0 = (self.llm.uso.chamadas, self.llm.uso.tokens_entrada, self.llm.uso.tokens_saida)

        def fechar(r: RespostaFinal) -> RespostaFinal:
            r.tempo_s = round(time.time() - inicio, 2)
            r.chamadas_llm = self.llm.uso.chamadas - uso0[0]
            r.tokens_entrada = self.llm.uso.tokens_entrada - uso0[1]
            r.tokens_saida = self.llm.uso.tokens_saida - uso0[2]
            return r

        try:
            plano: Plano = self.orquestrador.planejar(pergunta, historico)
        except CotaDiariaEsgotada:
            raise                                              # quem chama decide (ex.: avaliação para e salva)
        except Exception as e:                                 # noqa: BLE001
            return fechar(RespostaFinal(pergunta, "erro", f"Não consegui acessar o modelo de linguagem: {e}",
                                        erro=str(e)))

        if plano.classificacao != "dados":
            texto = plano.mensagem_usuario or (
                "Desculpe, só consigo responder perguntas sobre transações imobiliárias da cidade de "
                "São Paulo em 2024 e 2025 (quantidade, valores, preço do m², financiamento, bairros e zonas)."
                if plano.classificacao == "fora_escopo" else
                "Pode detalhar um pouco mais? Por exemplo, qual métrica (preço do m², volume, número de vendas) "
                "e qual período você quer analisar?")
            return fechar(RespostaFinal(pergunta, plano.classificacao, texto, suposicoes=plano.suposicoes))

        res = self.agente_sql.executar(plano, pergunta)
        base = dict(pergunta=pergunta, classificacao="dados", sql=res.sql, suposicoes=plano.suposicoes,
                    tentativas_sql=res.tentativas, sql_valido_primeira=res.valido_primeira,
                    historico_sql=res.historico_sql)
        if not res.ok:
            return fechar(RespostaFinal(
                texto=("Não consegui montar uma consulta válida para essa pergunta depois de "
                       f"{res.tentativas} tentativa(s). Último erro: {res.erro}. "
                       "Tente reformular indicando a métrica e o período."),
                erro=res.erro, **base))

        texto = self.analista.responder(pergunta, plano, res)
        return fechar(RespostaFinal(texto=texto, colunas=res.colunas, linhas=res.linhas,
                                    avisos=res.avisos, **base))


if __name__ == "__main__":
    pergunta = " ".join(sys.argv[1:]) or "Quantas vendas tivemos em 2025?"
    r = AnalistaVirtual().responder(pergunta)
    print(f"[{r.classificacao}] {r.tempo_s}s, {r.tentativas_sql} tentativa(s), {r.chamadas_llm} chamadas LLM\n")
    print(r.texto)
    if r.sql:
        print("\nSQL usado:\n" + r.sql)
    if r.linhas:
        print("\n" + " | ".join(r.colunas))
        for linha in r.linhas[:20]:
            print(" | ".join(str(x) for x in linha))
