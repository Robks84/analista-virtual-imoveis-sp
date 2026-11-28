"""
Interface do analista virtual em Gradio (funciona no Google Colab e localmente).

    python src/app_gradio.py            # local, abre em http://127.0.0.1:7860
    # no Colab: seção 7 do notebook (gera um link público https://....gradio.live)
"""
from __future__ import annotations

import sys
from pathlib import Path

import gradio as gr
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import AnalistaVirtual  # noqa: E402
from tools import make_chart              # noqa: E402
from ui_textos import (AVISO_MEMORIA, COLUNAS_DETALHE, COLUNAS_RESUMO, COMO_FUNCIONA,  # noqa: E402
                       EXEMPLOS, SUBTITULO, TITULO, carregar_avaliacao, escopo)

_sistema: AnalistaVirtual | None = None


def sistema() -> AnalistaVirtual:
    global _sistema
    if _sistema is None:
        _sistema = AnalistaVirtual()
    return _sistema


def _lembranca(r) -> dict:
    """O que o orquestrador precisa lembrar de cada resposta para entender continuações ('e em 2023?')."""
    if r.classificacao == "dados":
        conteudo = f"Respondi à pergunta: {r.pergunta_reformulada or r.pergunta}. Resumo: {r.texto[:250]}"
    else:
        conteudo = r.texto[:400]          # recusa ou pedido de esclarecimento
    return {"role": "assistant", "content": conteudo}


def perguntar(pergunta: str, historico: list, memoria: list):
    historico, memoria = historico or [], memoria or []
    if not pergunta or not pergunta.strip():
        return historico, None, "", None, "", memoria
    try:
        r = sistema().responder(pergunta, memoria[-6:])      # últimas 3 trocas
    except Exception as e:  # noqa: BLE001
        historico = historico + [{"role": "user", "content": pergunta},
                                 {"role": "assistant", "content": f"Erro: {e}"}]
        return historico, None, "", None, "", memoria

    historico = historico + [{"role": "user", "content": pergunta},
                             {"role": "assistant", "content": r.texto}]
    memoria = memoria + [{"role": "user", "content": pergunta}, _lembranca(r)]
    tabela = pd.DataFrame(r.linhas, columns=r.colunas) if r.linhas else None
    grafico = make_chart(r.colunas, r.linhas, r.pergunta_reformulada or pergunta) if r.linhas else None
    info = f"⏱ {r.tempo_s} s · {r.chamadas_llm} chamadas ao LLM · {r.tentativas_sql} execução(ões) de SQL"
    if r.pergunta_reformulada and r.pergunta_reformulada.strip() != pergunta.strip():
        info += f" · entendi como: *{r.pergunta_reformulada}*"
    if r.suposicoes:
        info += " · suposições: " + "; ".join(r.suposicoes)
    return historico, tabela, r.sql or "-- (sem SQL: pergunta fora de escopo ou ambígua)", grafico, info, memoria


def criar_interface() -> gr.Blocks:
    resumo, detalhe = carregar_avaliacao()
    with gr.Blocks(title="Analista Virtual — Imóveis SP", theme=gr.themes.Soft()) as demo:
        with gr.Row():
            # ------------------------------------------------ lateral
            with gr.Column(scale=1, min_width=260):
                gr.Markdown("## 🏙️ Analista Virtual\nSistema multiagente de BI em **Python + SQLite + LLM** "
                            "sobre as transações imobiliárias de **São Paulo**.")
                gr.Markdown(escopo())
                gr.Markdown("### Perguntas de exemplo")
                botoes = [gr.Button(ex, size="sm") for ex in EXEMPLOS]
            # ------------------------------------------------ conteúdo
            with gr.Column(scale=3):
                gr.Markdown(f"# {TITULO}\n{SUBTITULO}")
                with gr.Tabs():
                    with gr.Tab("💬 Bate-papo"):
                        gr.Markdown(f"ℹ️ {AVISO_MEMORIA}")
                        chat = gr.Chatbot(type="messages", height=360, label="Conversa")
                        memoria = gr.State([])
                        with gr.Row():
                            caixa = gr.Textbox(placeholder="Ex.: Qual o preço médio do m² em Pinheiros em 2025?",
                                               scale=5, show_label=False)
                            botao = gr.Button("Perguntar", variant="primary", scale=1)
                        nova = gr.Button("🧹 Nova conversa", size="sm")
                        info = gr.Markdown()
                        with gr.Tab("Tabela"):
                            tabela = gr.Dataframe()
                        with gr.Tab("Gráfico"):
                            grafico = gr.Plot()
                        sql = gr.Code(label="SQL usado", language="sql")
                    with gr.Tab("🧭 Como funciona"):
                        gr.Markdown(COMO_FUNCIONA)
                    with gr.Tab("📈 Avaliação"):
                        gr.Markdown("### Golden set: 20 perguntas com resposta conhecida\n"
                                    "7 fáceis, 7 médias, 4 difíceis e 2 armadilhas. A rodada **antes** mede o "
                                    "sistema original; a rodada **depois**, o sistema com as correções da "
                                    "análise de erros (`eval/analise_erros.md`).")
                        if resumo:
                            gr.Dataframe(pd.DataFrame(resumo, columns=COLUNAS_RESUMO), label="Resumo")
                            gr.Dataframe(pd.DataFrame(detalhe, columns=COLUNAS_DETALHE),
                                         label="Pergunta a pergunta", wrap=True)
                        else:
                            gr.Markdown("_Ainda não há resultados. Rode `python eval/run_eval.py --rodada antes`._")

        entradas = [caixa, chat, memoria]
        saidas = [chat, tabela, sql, grafico, info, memoria]
        botao.click(perguntar, entradas, saidas).then(lambda: "", None, caixa)
        caixa.submit(perguntar, entradas, saidas).then(lambda: "", None, caixa)
        nova.click(lambda: ([], None, "", None, "", []), None, saidas)
        for b in botoes:   # clicar num exemplo faz a pergunta
            b.click(lambda ex: ex, b, caixa).then(perguntar, entradas, saidas).then(lambda: "", None, caixa)
    return demo


if __name__ == "__main__":
    criar_interface().launch(share="--share" in sys.argv)
