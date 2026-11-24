"""
Os 3 agentes do analista virtual.

    1. Orquestrador  -> classifica a pergunta (dados | fora_escopo | ambigua) e busca contexto
                        na camada semântica com get_schema() / get_metric()
    2. Agente SQL    -> escreve o SQL, valida com validate_sql() e executa com run_sql()
                        (no máximo 3 tentativas de execução)
    3. Analista      -> transforma o resultado em resposta de negócio

O cliente de LLM usa a API "OpenAI-compatível", que funciona com OpenAI, Gemini,
Groq, Ollama e outros só trocando LLM_BASE_URL / LLM_MODEL no .env.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from tools import (FUNCOES, TOOLS_ORQUESTRADOR, TOOLS_SQL, exemplos_sql, get_metric, get_schema,
                   sinonimos_de_valores)

MAX_TENTATIVAS_SQL = 3


# ---------------------------------------------------------------------------
# Cliente de LLM
# ---------------------------------------------------------------------------
class CotaDiariaEsgotada(RuntimeError):
    """O provedor recusou por limite DIÁRIO (free tier). Só volta a funcionar no dia seguinte."""


@dataclass
class Uso:
    chamadas: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0


class LLM:
    """Wrapper fino sobre a API de chat completions com tool calling."""

    def __init__(self, client: Any | None = None, model: str | None = None):
        if client is None:
            from dotenv import load_dotenv
            from openai import OpenAI
            load_dotenv()
            api_key = os.getenv("LLM_API_KEY")
            if not api_key:
                raise RuntimeError("Defina LLM_API_KEY no arquivo .env (veja .env.example).")
            # max_retries=0: as novas tentativas são controladas aqui (ver chat()), com mensagens claras
            client = OpenAI(api_key=api_key, base_url=os.getenv("LLM_BASE_URL") or None, timeout=90,
                            max_retries=0)
        self.client = client
        self.model = model or os.getenv("LLM_MODEL", "gemini-4-hydrogen")
        base = str(getattr(client, "base_url", "") or os.getenv("LLM_BASE_URL", ""))
        self.gemini = "googleapis" in base or self.model.startswith("gemini")
        self.reasoning_effort = os.getenv("LLM_REASONING_EFFORT")   # opcional: low | medium | high
        self.uso = Uso()

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        kwargs = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            kwargs["tools"] = tools
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        max_tentativas = 6
        for tentativa in range(max_tentativas):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                break
            except Exception as e:              # noqa: BLE001
                status = getattr(e, "status_code", None)
                texto = str(e).lower()
                if status == 429 and any(k in texto for k in ("per day", "perday", "(tpd)", "(rpd)")):
                    raise CotaDiariaEsgotada(str(e)) from e      # cota do dia acabou: não adianta esperar
                if status is not None and 400 <= status < 500 and status != 429:
                    raise                       # erro na requisição: não adianta repetir
                if tentativa == max_tentativas - 1:
                    raise
                m = re.search(r"(?:try again|retry) in ([\d.]+)\s*(ms|s)", texto)
                espera = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1) + 1) if m else 10 * (tentativa + 1)
                print(f"   [LLM] {status or 'erro de rede'}: aguardando {espera:.0f}s e tentando de novo...")
                time.sleep(min(espera, 90))
        self.uso.chamadas += 1
        if getattr(resp, "usage", None):
            self.uso.tokens_entrada += resp.usage.prompt_tokens or 0
            self.uso.tokens_saida += resp.usage.completion_tokens or 0
        return resp.choices[0].message


SKIP_SIGNATURE = "skip_thought_signature_validator"   # valor aceito pelo Gemini quando não há assinatura


def _extra_content(tc) -> dict | None:
    """Campo não padrão 'extra_content' (Gemini guarda nele a thought_signature)."""
    extra = getattr(tc, "extra_content", None)
    if extra is None and getattr(tc, "model_extra", None):
        extra = tc.model_extra.get("extra_content")
    if extra is not None and hasattr(extra, "model_dump"):
        extra = extra.model_dump()
    return extra


def _msg_assistente(msg, gemini: bool = False) -> dict:
    """Converte a resposta do modelo em mensagem de histórico.

    Modelos Gemini com "thinking" exigem que a thought_signature de cada chamada de
    ferramenta volte no histórico (tool_calls[i].extra_content.google.thought_signature);
    sem ela a API responde erro 400.
    """
    d = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        calls = []
        for i, tc in enumerate(msg.tool_calls):
            c = {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            extra = _extra_content(tc)
            if extra:
                c["extra_content"] = extra
            elif gemini and i == 0:
                c["extra_content"] = {"google": {"thought_signature": SKIP_SIGNATURE}}
            calls.append(c)
        d["tool_calls"] = calls
    return d


def _extrair_json(texto: str) -> dict:
    texto = (texto or "").strip()
    texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.M).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _resultado_vazio(out: dict) -> bool:
    """Sem linhas, ou todas as células NULL (ex.: AVG de um filtro que não casou nada)."""
    linhas = out.get("linhas") or []
    return not linhas or all(v is None for linha in linhas for v in linha)


def _extrair_sql(texto: str) -> str:
    """Procura um SQL no texto do modelo (JSON {"sql": ...}, bloco ```sql``` ou SELECT/WITH solto)."""
    d = _extrair_json(texto)
    if isinstance(d.get("sql"), str) and d["sql"].strip():
        return d["sql"].strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", texto or "", flags=re.S | re.I)
    if m and re.match(r"\s*(SELECT|WITH)\b", m.group(1), flags=re.I):
        return m.group(1).strip()
    m = re.search(r"\b(WITH|SELECT)\b.*", texto or "", flags=re.S | re.I)
    return m.group(0).strip() if m else ""


def _dump(obj: Any, limite: int = 12_000) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limite else s[:limite] + "...(truncado)"


# ---------------------------------------------------------------------------
# 1. ORQUESTRADOR
# ---------------------------------------------------------------------------
PROMPT_ORQUESTRADOR = """Você é o ORQUESTRADOR de um analista virtual de dados do mercado imobiliário
da cidade de São Paulo (transações com ITBI pago, jan/2024 a dez/2025).

Sua tarefa:
1. Classificar a pergunta do usuário em UMA categoria:
   - "dados": pode ser respondida com consulta à base.
   - "fora_escopo": não tem relação com a base (previsões, recomendações de investimento,
     aluguel, dados de pessoas, outras cidades, períodos fora de 2024–2025, assuntos gerais).
   - "ambigua": é sobre a base, mas falta informação essencial para escolher a métrica ou o
     recorte (ex.: "melhores bairros" sem dizer o critério). Na dúvida entre assumir e perguntar,
     só pergunte se respostas razoáveis forem MUITO diferentes; senão assuma e registre a suposição.
2. Se for "dados", chame get_schema UMA vez (sem argumentos: devolve o resumo das tabelas, métricas e
   regras) e, se precisar, get_metric para a métrica pedida. Não repita chamadas. Se for "fora_escopo"
   ou "ambigua", responda direto, sem ferramentas.
3. Responder SOMENTE com um JSON, sem texto fora dele:
{
  "classificacao": "dados" | "fora_escopo" | "ambigua",
  "mensagem_usuario": "para fora_escopo: recusa educada explicando o que o sistema responde; para ambigua: pergunta de esclarecimento com 2 ou 3 opções; para dados: vazio",
  "pergunta_reformulada": "a pergunta reescrita de forma completa e sem ambiguidade (com período explícito)",
  "tabelas": ["fato_transacoes", "..."],
  "metricas": ["nome_da_metrica_oficial"],
  "filtros_e_regras": ["regras de negócio e filtros que o SQL DEVE aplicar"],
  "suposicoes": ["suposições feitas para interpretar a pergunta"]
}"""


@dataclass
class Plano:
    classificacao: str
    mensagem_usuario: str = ""
    pergunta_reformulada: str = ""
    tabelas: list[str] = field(default_factory=list)
    metricas: list[str] = field(default_factory=list)
    filtros_e_regras: list[str] = field(default_factory=list)
    suposicoes: list[str] = field(default_factory=list)


class Orquestrador:
    def __init__(self, llm: LLM, max_passos: int = 6):
        self.llm, self.max_passos = llm, max_passos

    def planejar(self, pergunta: str, historico: list[dict] | None = None) -> Plano:
        msgs = [{"role": "system", "content": PROMPT_ORQUESTRADOR}]
        for h in (historico or [])[-6:]:                 # memória curta (para esclarecimentos)
            msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": pergunta})

        for _ in range(self.max_passos):
            msg = self.llm.chat(msgs, TOOLS_ORQUESTRADOR)
            msgs.append(_msg_assistente(msg, self.llm.gemini))
            if not getattr(msg, "tool_calls", None):
                break
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                try:
                    out = FUNCOES[tc.function.name](**args)
                except Exception as e:                   # noqa: BLE001
                    out = {"erro": str(e)}
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": _dump(out)})
        else:
            msgs.append({"role": "user", "content": "Responda agora apenas com o JSON final."})
            msg = self.llm.chat(msgs)

        d = _extrair_json(msg.content)
        if d.get("classificacao") not in {"dados", "fora_escopo", "ambigua"}:
            d = {"classificacao": "dados", "pergunta_reformulada": pergunta}   # fallback seguro
        campos = Plano.__dataclass_fields__
        return Plano(**{k: v for k, v in d.items() if k in campos})


# ---------------------------------------------------------------------------
# 2. AGENTE SQL
# ---------------------------------------------------------------------------
PROMPT_SQL = """Você é o AGENTE SQL. Escreve consultas SQLite corretas para responder perguntas de negócio.

Regras:
- Use SOMENTE as tabelas e colunas do contexto. Dialeto: SQLite (sem MEDIAN, sem ILIKE, sem EXTRACT).
- Sempre use aliases: fato_transacoes f, dim_tempo t, dim_local l, dim_uso u, dim_natureza n,
  dim_financiamento fi, dim_padrao p.
- Aplique TODOS os filtros e regras de negócio do contexto e use as definições oficiais das métricas.
- Dê nomes claros às colunas de saída (ex.: qtd_vendas, preco_medio_m2, variacao_pct).
- Arredonde valores monetários com ROUND(x, 2) e percentuais com ROUND(x, 1).
- Retorne só as colunas necessárias para responder. Para rankings use ORDER BY + LIMIT.
- Fluxo: chame run_sql DIRETAMENTE com o SQL final (ele já valida antes de executar). Se voltar erro,
  leia a mensagem, corrija e chame run_sql de novo. Máximo de 3 execuções. validate_sql é opcional.
- Quando run_sql funcionar, o processo termina automaticamente: não é preciso responder mais nada."""


@dataclass
class ResultadoSQL:
    ok: bool
    sql: str = ""
    colunas: list[str] = field(default_factory=list)
    linhas: list[list[Any]] = field(default_factory=list)
    erro: str = ""
    avisos: list[str] = field(default_factory=list)
    tentativas: int = 0                 # nº de execuções de run_sql
    valido_primeira: bool = False       # a 1ª execução funcionou?
    historico_sql: list[dict] = field(default_factory=list)


def montar_contexto(plano: Plano) -> str:
    """Contexto que o Agente SQL recebe: recorte da camada semântica + plano do orquestrador."""
    tabelas = list(dict.fromkeys(["fato_transacoes"] + [t for t in plano.tabelas if t]))
    schema = get_schema(tabelas)
    metricas = {}
    for m in plano.metricas:
        d = get_metric(m)
        if "erro" not in d:
            metricas[d["metrica"]] = {k: d[k] for k in ("descricao", "sql", "filtro") if k in d}
    ctx = {
        "tabelas": {t: v["colunas"] for t, v in schema["tabelas"].items()},
        "relacionamentos": schema["relacionamentos"],
        "metricas": metricas,
        "regras_de_negocio": schema["regras_de_negocio"],
        "sinonimos_de_valores (use estes filtros exatos)": sinonimos_de_valores(),
        "filtros_e_regras_desta_pergunta": plano.filtros_e_regras,
        "suposicoes": plano.suposicoes,
        "exemplos": [e["sql"].strip() for e in exemplos_sql()[:2]],
    }
    return yaml.safe_dump(ctx, allow_unicode=True, sort_keys=False, width=200)


class AgenteSQL:
    def __init__(self, llm: LLM, max_passos: int = 8):
        self.llm, self.max_passos = llm, max_passos

    def executar(self, plano: Plano, pergunta_original: str) -> ResultadoSQL:
        msgs = [
            {"role": "system", "content": PROMPT_SQL},
            {"role": "user", "content": (
                f"CONTEXTO (camada semântica):\n{montar_contexto(plano)}\n\n"
                f"PERGUNTA ORIGINAL: {pergunta_original}\n"
                f"PERGUNTA REFORMULADA: {plano.pergunta_reformulada or pergunta_original}")},
        ]
        res = ResultadoSQL(ok=False)
        ultimo_ok: dict | None = None
        vazio_pendente = False

        for _ in range(self.max_passos):
            msg = self.llm.chat(msgs, TOOLS_SQL)
            msgs.append(_msg_assistente(msg, self.llm.gemini))
            if not getattr(msg, "tool_calls", None):
                break
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                nome = tc.function.name
                if nome == "run_sql":
                    if res.tentativas >= MAX_TENTATIVAS_SQL:
                        out = {"ok": False, "erro": "Limite de 3 tentativas atingido. Pare e responda."}
                    else:
                        res.tentativas += 1
                        out = FUNCOES["run_sql"](args.get("sql", ""))
                        if res.tentativas == 1:
                            res.valido_primeira = bool(out.get("ok"))
                        res.historico_sql.append({"sql": args.get("sql", ""), "ok": out.get("ok"),
                                                  "erro": out.get("erro", "")})
                        if out.get("ok"):
                            ultimo_ok = out
                            if _resultado_vazio(out) and res.tentativas < MAX_TENTATIVAS_SQL:
                                vazio_pendente = True
                                out = {**out, "aviso": (
                                    "A consulta rodou mas voltou VAZIA ou só com NULL. Quase sempre é um filtro "
                                    "de texto com valor inexistente. Revise os filtros usando os sinônimos de "
                                    "valores / códigos do contexto e chame run_sql de novo. Se o vazio for a "
                                    "resposta correta, repita o mesmo SQL.")}
                            elif vazio_pendente and _resultado_vazio(out):
                                vazio_pendente = False        # confirmou o vazio
                            else:
                                vazio_pendente = False
                    # para a LLM, devolve só uma amostra do resultado
                    visivel = dict(out)
                    if "linhas" in visivel:
                        visivel["linhas"] = visivel["linhas"][:30]
                    conteudo = _dump(visivel)
                elif nome == "validate_sql":
                    conteudo = _dump(FUNCOES["validate_sql"](args.get("sql", "")))
                else:
                    conteudo = _dump({"erro": f"Ferramenta desconhecida: {nome}"})
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": conteudo})
            if ultimo_ok is not None and not vazio_pendente:
                break                       # resultado obtido: economiza uma chamada ao LLM
            if res.tentativas >= MAX_TENTATIVAS_SQL:
                break

        # Plano B: alguns modelos escrevem o SQL no texto em vez de chamar a ferramenta
        if ultimo_ok is None and res.tentativas == 0 and getattr(msg, "content", None):
            sql_texto = _extrair_sql(msg.content)
            if sql_texto:
                res.tentativas = 1
                out = FUNCOES["run_sql"](sql_texto)
                res.valido_primeira = bool(out.get("ok"))
                res.historico_sql.append({"sql": sql_texto, "ok": out.get("ok"), "erro": out.get("erro", "")})
                if out.get("ok"):
                    ultimo_ok = out

        if ultimo_ok:
            res.ok, res.sql = True, ultimo_ok["sql"]
            res.colunas, res.linhas, res.avisos = ultimo_ok["colunas"], ultimo_ok["linhas"], ultimo_ok["avisos"]
        else:
            res.erro = (res.historico_sql[-1]["erro"] if res.historico_sql
                        else "O agente não conseguiu gerar uma consulta.")
            res.sql = res.historico_sql[-1]["sql"] if res.historico_sql else ""
        return res


# ---------------------------------------------------------------------------
# 3. ANALISTA
# ---------------------------------------------------------------------------
PROMPT_ANALISTA = """Você é o AGENTE ANALISTA de um time de BI imobiliário. Recebe a pergunta, o SQL executado
e o resultado, e escreve a resposta para um gestor, em português do Brasil.

Formato (markdown curto, sem repetir a tabela inteira):
1. **Resposta direta** em 1–2 frases com os números principais (R$ com separador brasileiro,
   ex.: R$ 1.234.567,89; percentuais com 1 casa decimal).
2. **Destaques**: até 3 bullets com comparações, tendências ou extremos que aparecem no resultado.
3. **Ressalvas**: só as relevantes (ex.: preço do m² considera só imóveis residenciais vendidos inteiros;
   área construída inclui áreas comuns; datas pelo mês de pagamento do ITBI; amostras pequenas;
   valores nominais; zona aproximada pelo CEP).
4. **Próxima pergunta sugerida**: 1 pergunta que aprofunde a análise.

Use SOMENTE os números do resultado. Não invente dados. Se o resultado estiver vazio, diga isso
e sugira como reformular."""


class Analista:
    def __init__(self, llm: LLM):
        self.llm = llm

    def responder(self, pergunta: str, plano: Plano, res: ResultadoSQL) -> str:
        conteudo = {
            "pergunta": pergunta,
            "suposicoes": plano.suposicoes,
            "sql": res.sql,
            "colunas": res.colunas,
            "linhas": res.linhas[:30],
            "total_linhas": len(res.linhas),
            "avisos_dos_guardrails": res.avisos,
        }
        msg = self.llm.chat([{"role": "system", "content": PROMPT_ANALISTA},
                             {"role": "user", "content": _dump(conteudo)}])
        return msg.content or ""
