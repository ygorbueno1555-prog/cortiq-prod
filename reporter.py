"""reporter.py — Cortiq Decision Copilot v2
Generates structured investment reports via Claude (async streaming).
"""
import os
from typing import AsyncGenerator, List, Dict, Optional
from anthropic import AsyncAnthropic

EQUITY_PROMPT = """\
Você é o analista-chefe de uma boutique de research independente tier-1. Você passou 25 anos \
em sell-side de alto nível (Goldman, Morgan Stanley, Itaú BBA) e agora presta serviço para \
gestores de portfólio exigentes. Você não lista fatos — você transforma dados em decisão.

Seu trabalho agora: preparar um briefing de decisão sobre {ticker} para o gestor.

CONTEXTO DO GESTOR:
- Tese atual: {thesis}
- Mandato: {mandate}
{prev_context}
{market_data_section}
PESQUISAS DISPONÍVEIS (cite [N] em cada afirmação):
{research}

---
PRINCÍPIOS:
- Cada seção deve responder "por que isso importa para o gestor", não só "o que aconteceu"
- Use linguagem direta e opiniosa — o gestor quer sua leitura, não uma lista de notícias
- Cite [N] em toda afirmação com dado concreto
- Inferências: use "estimo" ou "provável" — nunca afirme sem evidência
- Se falta dado crítico, diga qual e por que faz falta

Gere o relatório EXATAMENTE neste formato:

## VEREDITO
**[TESE MANTIDA | TESE ALTERADA | TESE INVALIDADA]**
Confiança: [ALTA | MÉDIA | BAIXA]
[1 frase explicando a confiança em termos de qualidade das evidências encontradas]
[1-2 frases de racional — opiniosas, conectando o momento do ativo com a tese do gestor]

## AÇÃO RECOMENDADA
**[COMPRAR | MANTER | REDUZIR | VENDER]**
[2-3 frases explicando o porquê com dados [N]. Seja direto: "a assimetria risco/retorno atual sugere X porque Y"]

{dynamic_section}

## O QUE VOCÊ PRECISA MONITORAR
[Não é lista de fatos — é o que o gestor deve ter no radar. Para cada item, explique o gatilho e o que ele muda na decisão]
- **[tema 1]**: [o que está acontecendo] → [o que muda na tese se evoluir para X]
- **[tema 2]**: [o que está acontecendo] → [o que muda na tese se evoluir para X]
- **[tema 3]**: [o que está acontecendo] → [o que muda na tese se evoluir para X]

## CATALISADORES (30–90 dias)
- **[evento]**: [data estimada se disponível] — [impacto esperado no papel e por quê [N]]
- **[evento]**: [por que é relevante agora especificamente]

## RISCOS QUE INVALIDAM A TESE
- **[risco]**: [o que está em jogo] — gatilho de invalidação: [fato concreto que mudaria o veredito]
- **[risco]**: [o que está em jogo] — gatilho de invalidação: [fato concreto que mudaria o veredito]

## LEITURA DE PORTFÓLIO
[Conecte o ativo ao mandato do gestor. Se mandato não informado, escreva a análise considerando um portfólio fundamentalista de longo prazo com foco em risco/retorno. Dê uma posição concreta: tamanho sugerido, momento de entrada, ponto de stop conceitual]

## TRILHA DE EVIDÊNCIAS
- **[N]** [título] — [afirmação suportada] — [URL]

## EXPLORAR TAMBÉM
- **[ticker 1]** — [por que comparar agora]
- **[ticker 2]** — [por que comparar agora]
- **[ticker 3]** — [por que comparar agora]
"""

STARTUP_PROMPT = """\
Você é sócio de um fundo tier-1 de venture capital. Você já avaliou mais de 500 startups e \
participou de rodadas de Series A ao IPO. Seu trabalho agora: preparar um VC memo de decisão \
sobre {name} para o comitê de investimento.

Você não escreve relatórios neutros — você dá uma posição clara e defende com evidências.

TESE DE INVESTIMENTO: {thesis}
SITE: {url}
{prev_context}
PESQUISAS (cite [N] em cada afirmação):
{research}

---
PRINCÍPIOS:
- Cada seção deve responder "por que isso importa para a decisão de investimento"
- Seja opinionado: o comitê quer sua leitura, não um resumo da internet
- Cite [N] em dados concretos. Inferências: "estimo" ou "provável"
- Se falta dado crítico, aponte qual e o que ele mudaria na decisão

Gere o VC memo EXATAMENTE neste formato:

## VEREDITO
**[INVESTIR | MONITORAR | PASSAR]**
Confiança: [ALTA | MÉDIA | BAIXA]
[1 frase sobre a qualidade das evidências disponíveis]
[1-2 frases de racional — diretas, com sua posição sobre o negócio]

## O QUE É E POR QUE IMPORTA AGORA
[3-4 frases: o que fazem, para quem, qual o diferencial real vs. o pitch, por que o timing é (ou não é) favorável agora]

{dynamic_section}

## TIME — MINHA LEITURA
- **O que me convence**: [dados concretos [N] sobre os founders — track record, execução, coerência]
- **O que me preocupa**: [lacunas reais, não genéricas — o que falta para esse momento específico]

## MERCADO — REALIDADE VS. PITCH
- **Tamanho real endereçável**: [não o TAM total — o mercado que eles conseguem capturar agora [N]]
- **Crescimento**: [taxa com citação [N] ou "sem dados primários — estimo X com base em Y"]
- **Timing**: [Por que agora? O que mudou no mercado que abre essa janela?]

## TRAÇÃO — O QUE PROVAM OS NÚMEROS
[Separe evidência real de marketing. Para cada métrica: o que ela prova, o que ela não prova]
- [métrica/sinal 1 [N] → o que isso significa de verdade]
- [métrica/sinal 2 → o que está faltando ver]

## COMPETIÇÃO — ONDE ELES GANHAM E PERDEM
- **[concorrente principal]**: [vantagem real desta startup vs. esse player]
- **[ameaça]**: [quem pode matar esse negócio em 18 meses e como]

## RED FLAGS — O QUE ME INCOMODA
- **[flag 1]**: [o que vi/não vi nas pesquisas que levanta dúvida]
- **[flag 2]**: [padrão de risco que reconheço de deals anteriores]

## A APOSTA
**Bull case**: [se tudo der certo, o que acontece e qual o retorno potencial]
**Bear case**: [o cenário realista de falha e por quê]
**O que preciso ver para mudar de posição**: [métrica ou evento concreto]

## GATILHOS DE INVALIDAÇÃO
- [fato específico que mudaria INVESTIR → PASSAR imediatamente]
- [milestone que, se não atingido em X meses, confirma o bear case]

## PRÓXIMAS PERGUNTAS PARA OS FOUNDERS
- [pergunta 1 — a mais importante, que você não conseguiu responder pelas pesquisas]
- [pergunta 2]

## TRILHA DE EVIDÊNCIAS
- **[N]** [título] — [afirmação suportada] — [URL]

## COMPARAR COM
- **[startup 1]** — [por que a comparação é relevante agora]
- **[startup 2]** — [por que a comparação é relevante agora]
- **[startup 3]** — [por que a comparação é relevante agora]
"""


def _format_research(results: List[Dict]) -> str:
    parts = []
    for i, r in enumerate(results[:20], 1):
        source_label = f"[{r.get('source_type', 'web')}]" if r.get('source_type') else ""
        parts.append(f"[{i}] {r['title']} {source_label}\n{r['content']}\nFonte: {r['url']}")
    return "\n\n".join(parts)


def _get_client() -> AsyncAnthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY não configurada")
    return AsyncAnthropic(api_key=api_key)


def _get_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _load_critic_rules() -> dict:
    import json
    base = os.getenv("CORTIQ_CONFIG_DIR", os.path.join(os.path.dirname(__file__), "config"))
    path = os.path.join(base, "critic_rules.json")
    if not os.path.exists(path):
        return {"max_bullets": 6}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


async def stream_equity_report(
    results: List[Dict],
    ticker: str,
    thesis: str,
    mandate: str,
    prev_verdict: str = "",
    prev_date: str = "",
    market_data: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    try:
        client = _get_client()
    except ValueError as e:
        yield f"## Erro de Configuração\n{e}"
        return

    from equity_data import format_market_data

    # Build comparison context if we have previous analysis
    prev_context = ""
    dynamic_section = ""
    if prev_verdict and prev_date:
        prev_context = f"\nANÁLISE ANTERIOR ({prev_date}): veredito **{prev_verdict}**. Use isso para identificar o que mudou.\n"
        dynamic_section = (
            f"## O QUE MUDOU DESDE {prev_date}\n"
            f"[Compare diretamente com o veredito anterior ({prev_verdict}). "
            f"Destaque apenas o que é novo ou diferente: fatos, riscos, catalisadores, mudança de momentum. "
            f"Não repita o que continua igual — foque no delta.]"
        )

    market_data_section = ""
    if market_data:
        formatted = format_market_data(market_data)
        if formatted:
            market_data_section = formatted + "\n\n"

    prompt = EQUITY_PROMPT.format(
        ticker=ticker,
        thesis=thesis or "análise fundamentalista geral",
        mandate=mandate or "portfólio fundamentalista de longo prazo",
        research=_format_research(results),
        prev_context=prev_context,
        dynamic_section=dynamic_section,
        market_data_section=market_data_section,
    )

    try:
        async with client.messages.stream(
            model=_get_model(),
            max_tokens=4000,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text
    except Exception as e:
        yield f"\n\n## Erro na Análise\n{e}"


async def stream_startup_report(
    results: List[Dict],
    name: str,
    url: str,
    thesis: str,
    prev_verdict: str = "",
    prev_date: str = "",
) -> AsyncGenerator[str, None]:
    try:
        client = _get_client()
    except ValueError as e:
        yield f"## Erro de Configuração\n{e}"
        return

    prev_context = ""
    dynamic_section = ""
    if prev_verdict and prev_date:
        prev_context = f"\nANÁLISE ANTERIOR ({prev_date}): veredito **{prev_verdict}**. Use para identificar o delta.\n"
        dynamic_section = (
            f"## O QUE MUDOU DESDE {prev_date}\n"
            f"[Foque no delta vs. veredito anterior ({prev_verdict}): tração nova, mudança de time, "
            f"funding, pivô, novos concorrentes, momentum. Ignore o que não mudou.]"
        )

    prompt = STARTUP_PROMPT.format(
        name=name,
        url=url or "não informado",
        thesis=thesis or "avaliar potencial de investimento",
        research=_format_research(results),
        prev_context=prev_context,
        dynamic_section=dynamic_section,
    )

    try:
        async with client.messages.stream(
            model=_get_model(),
            max_tokens=4000,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text
    except Exception as e:
        yield f"\n\n## Erro na Análise\n{e}"


async def generate_critic_notes(
    mode: str,
    report: str,
    evidence: str,
    evaluation: Dict,
) -> str:
    try:
        client = _get_client()
    except ValueError as e:
        return f"Erro: {e}"

    rules = _load_critic_rules()
    max_bullets = rules.get("max_bullets", 6)
    missing = ", ".join(evaluation.get("missing_sections", [])) or "nenhuma"

    prompt = f"""
Você é um revisor crítico de research.

Contexto:
- mode: {mode}
- coverage_score: {evaluation.get('coverage_score')}
- evidence_score: {evaluation.get('evidence_score')}
- primary_source_ratio: {evaluation.get('primary_source_ratio')}
- missing_sections: {missing}

Tarefa:
- Aponte afirmações sem evidência forte
- Indique se a confiança parece superestimada
- Sinalize seções fracas
- Sugira até 2 queries extras

Responda em até {max_bullets} bullets curtos.

Relatório:
{report}

Evidências:
{evidence}
""".strip()

    try:
        msg = await client.messages.create(
            model=_get_model(),
            max_tokens=400,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Erro critic: {e}"


BRIEF_ENTRY_PROMPT = """\
Você é um analista financeiro sênior. Com base nas pesquisas abaixo sobre {name} ({mode}), \
gere um briefing matinal CONCISO para um profissional de investimentos.

PESQUISAS:
{research}

---
Gere EXATAMENTE neste formato (máximo 6 linhas):

**[TESE MANTIDA | TESE ALTERADA | TESE INVALIDADA | INVESTIR | MONITORAR | PASSAR]** | Confiança: [ALTA | MÉDIA | BAIXA]
[2-3 frases com fatos concretos: o que mudou recentemente, situação atual, dado principal]
⚠️ Monitorar: [principal risco ou gatilho de atenção hoje]

Use apenas dados das pesquisas. Se dados insuficientes, diga explicitamente.
"""


async def generate_brief_entry(
    results: List[Dict], name: str, mode: str
) -> str:
    """Generate a concise briefing entry (non-streaming)."""
    try:
        client = _get_client()
    except ValueError as e:
        return f"**ERRO** | {e}"

    prompt = BRIEF_ENTRY_PROMPT.format(
        name=name,
        mode=mode,
        research=_format_research(results[:8]),
    )

    try:
        msg = await client.messages.create(
            model=_get_model(),
            max_tokens=300,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"**ERRO** | {e}"
