"""
Interface do analista virtual (Streamlit).

    streamlit run src/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import AnalistaVirtual  # noqa: E402
from tools import DB_PATH, make_chart     # noqa: E402

st.set_page_config(page_title="Analista Virtual — Imóveis SP", page_icon="🏙️", layout="wide")

EXEMPLOS = [
    "Quantas vendas de apartamentos tivemos em 2025?",
    "Qual o preço médio do m² por zona em 2025?",
    "Top 10 bairros com maior volume financeiro em 2024",
    "Como evoluiu o número de vendas mês a mês em 2025?",
    "Qual o percentual de vendas financiadas em Moema em 2025?",
]


@st.cache_resource
def carregar_sistema() -> AnalistaVirtual:
    return AnalistaVirtual()


def mostrar_resposta(r: dict) -> None:
    st.markdown(r["texto"])
    if r.get("linhas"):
        df = pd.DataFrame(r["linhas"], columns=r["colunas"])
        aba_tabela, aba_grafico = st.tabs(["Tabela", "Gráfico"])
        with aba_tabela:
            st.dataframe(df, use_container_width=True, hide_index=True)
        with aba_grafico:
            fig = make_chart(r["colunas"], r["linhas"])
            if fig is not None:
                st.pyplot(fig)
            else:
                st.caption("Resultado sem formato adequado para gráfico.")
    if r.get("sql"):
        # Transparência: o SQL usado aparece SEMPRE
        with st.expander("SQL usado", expanded=False):
            st.code(r["sql"], language="sql")
    detalhes = [f"{r['tempo_s']} s", f"{r['chamadas_llm']} chamadas ao LLM"]
    if r.get("tentativas_sql"):
        detalhes.append(f"{r['tentativas_sql']} execução(ões) de SQL")
    if r.get("suposicoes"):
        detalhes.append("suposições: " + "; ".join(r["suposicoes"]))
    st.caption(" · ".join(detalhes))


# ---------------------------------------------------------------- barra lateral
with st.sidebar:
    st.header("🏙️ Analista Virtual")
    st.write("Pergunte sobre as **transações imobiliárias de São Paulo** com ITBI pago "
             "(jan/2024 a dez/2025, fonte: Prefeitura de SP).")
    st.subheader("Exemplos")
    for ex in EXEMPLOS:
        if st.button(ex, use_container_width=True):
            st.session_state["pergunta_exemplo"] = ex
    st.divider()
    if st.button("Limpar conversa", use_container_width=True):
        st.session_state["mensagens"] = []
        st.rerun()
    if not DB_PATH.exists():
        st.error("Banco não encontrado. Rode antes: python src/load_data.py")

# ---------------------------------------------------------------- conversa
st.title("Analista virtual do mercado imobiliário de SP")
st.session_state.setdefault("mensagens", [])

for m in st.session_state["mensagens"]:
    with st.chat_message(m["role"]):
        if m["role"] == "assistant":
            mostrar_resposta(m["resposta"])
        else:
            st.markdown(m["content"])

pergunta = st.chat_input("Ex.: Qual o preço médio do m² em Pinheiros em 2025?")
pergunta = pergunta or st.session_state.pop("pergunta_exemplo", None)

if pergunta:
    st.session_state["mensagens"].append({"role": "user", "content": pergunta})
    with st.chat_message("user"):
        st.markdown(pergunta)
    historico = [{"role": m["role"], "content": m["content"] if m["role"] == "user" else m["resposta"]["texto"]}
                 for m in st.session_state["mensagens"][:-1]]
    with st.chat_message("assistant"):
        with st.spinner("Consultando a camada semântica, gerando e validando o SQL..."):
            try:
                resposta = carregar_sistema().responder(pergunta, historico).to_dict()
            except Exception as e:  # noqa: BLE001
                resposta = {"texto": f"Erro: {e}", "tempo_s": 0, "chamadas_llm": 0}
        mostrar_resposta(resposta)
    st.session_state["mensagens"].append({"role": "assistant", "content": resposta["texto"], "resposta": resposta})
