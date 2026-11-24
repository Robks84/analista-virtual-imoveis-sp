"""
Avaliação objetiva do analista virtual com o golden set.

Uso:
    python eval/run_eval.py --rodada antes          # primeira medição
    # ... corrigir prompts/camada semântica com base no analise_erros.md ...
    python eval/run_eval.py --rodada depois         # segunda medição
    python eval/run_eval.py --rodada teste --ids F01 M02   # só algumas perguntas

Saídas:
    eval/resultados.csv       (uma linha por pergunta e por rodada; rodadas anteriores são mantidas)
    eval/resumo_metricas.md   (tabela de métricas por rodada e por nível)
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DB = ROOT / "data" / "processed" / "itbi.db"
GOLDEN = ROOT / "eval" / "golden_set.csv"
RESULTADOS = ROOT / "eval" / "resultados.csv"
RESUMO = ROOT / "eval" / "resumo_metricas.md"

CAMPOS = ["rodada", "id", "nivel", "tipo_esperado", "pergunta", "classificacao", "acertou", "motivo",
          "sql_gerado", "tentativas_sql", "sql_valido_1a", "tempo_s", "chamadas_llm",
          "tokens_entrada", "tokens_saida", "resposta", "erro"]


# ---------------------------------------------------------------------------
# Comparação de resultados (independente do nome das colunas)
# ---------------------------------------------------------------------------
def _norm(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = unicodedata.normalize("NFKD", str(v).strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    try:
        return float(s)
    except ValueError:
        return s


def _bate(a, b, tol: float = 0.01) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= max(tol * abs(a), 0.051)
    if isinstance(a, str) and isinstance(b, str):
        return a == b or a in b or b in a
    return a is None and b is None


def comparar(ref_linhas: list[list], sis_linhas: list[list]) -> tuple[bool, str]:
    """Acerta se o sistema devolve o mesmo nº de linhas e cada linha de referência
    tem uma linha correspondente no resultado do sistema contendo todos os seus valores
    (colunas extras no sistema são aceitas; ordem das colunas e nomes não importam)."""
    ref = [[_norm(v) for v in r] for r in ref_linhas]
    sis = [[_norm(v) for v in r] for r in sis_linhas]
    if len(ref) != len(sis):
        return False, f"nº de linhas diferente (esperado {len(ref)}, obtido {len(sis)})"
    usadas: set[int] = set()
    for r in ref:
        achou = False
        for j, s in enumerate(sis):
            if j in usadas:
                continue
            restantes = list(s)
            ok = True
            for v in r:
                idx = next((k for k, w in enumerate(restantes) if _bate(v, w)), None)
                if idx is None:
                    ok = False
                    break
                restantes.pop(idx)
            if ok:
                usadas.add(j)
                achou = True
                break
        if not achou:
            return False, f"linha esperada não encontrada: {r}"
    return True, "resultado confere"


# ---------------------------------------------------------------------------
def carregar_golden(ids: list[str] | None) -> list[dict]:
    with open(GOLDEN, encoding="utf-8") as fh:
        linhas = list(csv.DictReader(fh))
    return [g for g in linhas if not ids or g["id"] in ids]


def avaliar(g: dict, sistema, con) -> dict:
    r = sistema.responder(g["pergunta"])
    linha = {
        "id": g["id"], "nivel": g["nivel"], "tipo_esperado": g["tipo_esperado"], "pergunta": g["pergunta"],
        "classificacao": r.classificacao, "sql_gerado": r.sql, "tentativas_sql": r.tentativas_sql,
        "sql_valido_1a": "" if r.sql_valido_primeira is None or r.tentativas_sql == 0 else int(r.sql_valido_primeira),
        "tempo_s": r.tempo_s, "chamadas_llm": r.chamadas_llm, "tokens_entrada": r.tokens_entrada,
        "tokens_saida": r.tokens_saida, "resposta": r.texto.replace("\n", " ")[:500], "erro": r.erro,
    }
    if g["tipo_esperado"] != "dados":
        ok = r.classificacao in {"fora_escopo", "ambigua"}
        linha["acertou"], linha["motivo"] = int(ok), ("recusou/pediu esclarecimento" if ok
                                                     else f"respondeu como '{r.classificacao}'")
        return linha
    if r.classificacao != "dados":
        linha["acertou"], linha["motivo"] = 0, f"classificou como '{r.classificacao}'"
        return linha
    if not r.linhas and r.erro:
        linha["acertou"], linha["motivo"] = 0, "SQL falhou"
        return linha
    ref = con.execute(g["sql_referencia"]).fetchall()
    ok, motivo = comparar([list(x) for x in ref], r.linhas)
    linha["acertou"], linha["motivo"] = int(ok), motivo
    return linha


def resumo(linhas: list[dict]) -> dict:
    n = len(linhas)
    dados = [x for x in linhas if x["tipo_esperado"] == "dados" and str(x["tentativas_sql"]) not in ("", "0")]
    tent = [int(x["tentativas_sql"]) for x in dados]
    return {
        "perguntas": n,
        "acuracia_%": round(100 * sum(int(x["acertou"]) for x in linhas) / n, 1) if n else 0,
        "sql_valido_1a_%": round(100 * sum(int(x["sql_valido_1a"] or 0) for x in dados) / len(dados), 1) if dados else 0,
        "media_tentativas_correcao": round(sum(t - 1 for t in tent) / len(tent), 2) if tent else 0,
        "tempo_medio_s": round(sum(float(x["tempo_s"]) for x in linhas) / n, 2) if n else 0,
        "tokens_medios": round(sum(int(x["tokens_entrada"]) + int(x["tokens_saida"]) for x in linhas) / n) if n else 0,
    }


def escrever_resumo(todas: list[dict]) -> None:
    rodadas = list(dict.fromkeys(x["rodada"] for x in todas))
    cab = ["rodada", "recorte", "perguntas", "acuracia_%", "sql_valido_1a_%", "media_tentativas_correcao",
           "tempo_medio_s", "tokens_medios"]
    out = ["# Resumo das métricas do golden set", "",
           "| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
    for rod in rodadas:
        da_rodada = [x for x in todas if x["rodada"] == rod]
        recortes = [("geral", da_rodada)] + [(nv, [x for x in da_rodada if x["nivel"] == nv])
                                             for nv in ["facil", "medio", "dificil", "armadilha"]]
        for nome, grupo in recortes:
            if grupo:
                m = resumo(grupo)
                out.append("| " + " | ".join(str(v) for v in [rod, nome, *m.values()]) + " |")
    RESUMO.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rodada", default="antes", help="nome da rodada (ex.: antes, depois)")
    ap.add_argument("--ids", nargs="*", help="rodar só estas perguntas (ex.: F01 M03)")
    ap.add_argument("--continuar", action="store_true",
                    help="não repete perguntas que já têm resultado nesta rodada (útil após limite de cota)")
    ap.add_argument("--pausa", type=float, default=0, help="segundos de pausa entre perguntas")
    args = ap.parse_args()

    import time
    from agents import CotaDiariaEsgotada
    from orchestrator import AnalistaVirtual
    sistema = AnalistaVirtual()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    anteriores, ja_feitas = [], []
    if RESULTADOS.exists():
        with open(RESULTADOS, encoding="utf-8") as fh:
            for x in csv.DictReader(fh):
                if x["rodada"] != args.rodada:
                    anteriores.append(x)
                elif args.continuar and x["classificacao"] != "erro":
                    ja_feitas.append(x)             # perguntas que deram erro são refeitas
    feitas_ids = {x["id"] for x in ja_feitas}

    def salvar(novas):
        with open(RESULTADOS, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CAMPOS)
            w.writeheader()
            w.writerows(anteriores + ja_feitas + novas)

    novas = []
    pendentes = [g for g in carregar_golden(args.ids) if g["id"] not in feitas_ids]
    if feitas_ids:
        print(f"Continuando a rodada '{args.rodada}': {len(feitas_ids)} já feitas, {len(pendentes)} pendentes.")
    for i, g in enumerate(pendentes):
        try:
            linha = {"rodada": args.rodada, **avaliar(g, sistema, con)}
        except CotaDiariaEsgotada as e:
            salvar(novas)
            print(f"\n⚠️  Cota DIÁRIA do provedor esgotada em {g['id']}. Resultados até aqui foram salvos.\n"
                  f"   Amanhã (ou com outra chave/modelo) rode de novo com --continuar para terminar a rodada.\n"
                  f"   Detalhe: {str(e)[:300]}")
            return
        except Exception as e:  # noqa: BLE001 — uma pergunta com erro não derruba a avaliação
            linha = {"rodada": args.rodada, "id": g["id"], "nivel": g["nivel"], "tipo_esperado": g["tipo_esperado"],
                     "pergunta": g["pergunta"], "classificacao": "erro", "acertou": 0, "motivo": "exceção",
                     "sql_gerado": "", "tentativas_sql": 0, "sql_valido_1a": "", "tempo_s": 0, "chamadas_llm": 0,
                     "tokens_entrada": 0, "tokens_saida": 0, "resposta": "", "erro": str(e)[:500]}
        novas.append(linha)
        salvar(novas)                                   # salva a cada pergunta
        print(f"{'✔' if int(linha['acertou']) else '✘'} {g['id']} [{g['nivel']}] {linha['motivo']} "
              f"({linha['tempo_s']}s, {linha['tentativas_sql']} tentativa(s), "
              f"{int(linha['tokens_entrada']) + int(linha['tokens_saida'])} tokens)", flush=True)
        if args.pausa and i < len(pendentes) - 1:
            time.sleep(args.pausa)

    print(f"\nResultados salvos em {RESULTADOS.relative_to(ROOT)}\n")
    escrever_resumo(anteriores + ja_feitas + novas)


if __name__ == "__main__":
    main()
