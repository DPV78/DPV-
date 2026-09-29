"""Assistente estratégico (Claude) — discute decisões com base na estratégia de longo prazo do processo."""
import json
from collections.abc import Iterator
from datetime import date

import anthropic

from .config import settings
from .models import Case, Decision, Movement

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """Você é o assistente estratégico de um escritório de advocacia brasileiro e atua junto a uma \
pequena carteira de processos estratégicos, complexos e dinâmicos. Seu papel é o de um sócio experiente em \
contencioso que ajuda o advogado responsável a decidir, a cada andamento, qual medida tomar — sempre à luz da \
estratégia de longo prazo registrada para o processo.

Como trabalhar:
- Parta dos fatos do registro (andamentos, publicações, decisões anteriores, notas). Separe com clareza o que \
está no registro, o que é inferência sua e o que precisa ser confirmado nos autos.
- Relacione cada recomendação ao objetivo e à estratégia de longo prazo. Se um andamento contraria uma premissa \
da estratégia, diga isso explicitamente e proponha a revisão.
- Apresente as opções viáveis (inclusive não agir), com vantagens, riscos, custos, efeitos no cronograma e o \
que cada uma preserva ou compromete para as fases seguintes. Termine com a sua recomendação e o porquê.
- Aponte prazos possivelmente em curso e avise que a contagem precisa ser conferida nos autos e na intimação \
(termo inicial, dias úteis, feriados locais, prazos em dobro etc.). Nunca afirme que um prazo está correto sem \
ter os dados.
- Quando faltar informação para decidir, faça as perguntas necessárias antes de recomendar.
- A decisão final é sempre do advogado. Debata, discorde quando houver motivo e não apenas concorde.

Regras de precisão (inegociáveis):
- Não invente jurisprudência, súmulas, temas repetitivos, números de processos, artigos de lei ou doutrina. \
Cite dispositivos legais apenas quando tiver segurança; caso contrário, indique a matéria e recomende a \
verificação na fonte oficial. Nunca crie ementas ou números de julgados.
- Sinalize incerteza de forma explícita ("não tenho certeza", "verificar"). Avise quando algo pode ter mudado \
após o seu corte de conhecimento (alterações legislativas, mudanças de entendimento dos tribunais).
- Não preencha lacunas com suposições: pergunte.

Responda em português do Brasil, de forma objetiva e organizada (títulos curtos e listas quando ajudarem)."""


class AssistantUnavailable(Exception):
    pass


def _client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise AssistantUnavailable("ANTHROPIC_API_KEY não configurada")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + " […]"


def build_case_context(case: Case, focus_movement: Movement | None = None, max_movements: int = 40) -> str:
    parts = [
        f"# Processo {case.numero_cnj} — {case.titulo}",
        f"Tribunal: {case.tribunal.upper()} | Órgão: {case.orgao_julgador or '—'} | Classe: {case.classe or '—'}",
        f"Cliente: {case.cliente} (polo {case.polo}) | Parte contrária: {case.parte_contraria or '—'}",
        f"Fase: {case.fase or '—'} | Valor da causa: {case.valor_causa or '—'} | Prioridade: {case.prioridade}",
        f"Data de hoje: {date.today().strftime('%d/%m/%Y')}",
        "",
        "## Estratégia de longo prazo",
        f"Objetivo do cliente:\n{case.objetivo or '(não registrado)'}",
        f"\nEstratégia:\n{case.estrategia or '(não registrada)'}",
        f"\nPremissas:\n{case.premissas or '(não registradas)'}",
        f"\nRiscos mapeados:\n{case.riscos or '(não registrados)'}",
    ]

    decididas = [d for d in case.decisions if d.status == "decidida"][:10]
    if decididas:
        parts.append("\n## Decisões já tomadas (mais recentes primeiro)")
        for d in decididas:
            when = d.decided_at.strftime("%d/%m/%Y") if d.decided_at else ""
            parts.append(f"- {when} {d.titulo}: {_clip(d.decisao, 600)} — Fundamentos: {_clip(d.fundamentos, 600)}")

    pendentes = [s for s in case.steps if s.status == "pendente"]
    if pendentes:
        parts.append("\n## Próximos passos pendentes")
        for s in sorted(pendentes, key=lambda s: (s.prazo is None, s.prazo)):
            prazo = s.prazo.strftime("%d/%m/%Y") if s.prazo else "sem prazo"
            fatal = " (PRAZO FATAL)" if s.prazo_fatal else ""
            parts.append(f"- [{prazo}{fatal}] {s.descricao}")

    parts.append(f"\n## Andamentos (até {max_movements}, mais recentes primeiro)")
    for m in case.movements[:max_movements]:
        limit = 12000 if focus_movement is not None and m.id == focus_movement.id else 1500
        texto = _clip(m.texto, limit)
        parts.append(f"- {m.data.strftime('%d/%m/%Y')} [{m.fonte}] {m.titulo}" + (f"\n  {texto}" if texto else ""))

    if case.notes:
        parts.append("\n## Notas internas da equipe (mais recentes primeiro)")
        for n in case.notes[:15]:
            autor = n.author.name if n.author else "—"
            parts.append(f"- {n.created_at.strftime('%d/%m/%Y')} {autor}: {_clip(n.texto, 800)}")
    return "\n".join(parts)


def _decision_opening(case: Case, decision: Decision) -> str:
    ctx = build_case_context(case, focus_movement=decision.movement)
    header = [
        "<registro_do_processo>",
        ctx,
        "</registro_do_processo>",
        "",
        f"# Ponto de decisão: {decision.titulo}",
    ]
    if decision.movement:
        header.append(f"Andamento que motivou a decisão: {decision.movement.data.strftime('%d/%m/%Y')} — {decision.movement.titulo}")
    if decision.contexto:
        header.append(f"Contexto informado pela equipe:\n{decision.contexto}")
    return "\n".join(header)


def build_messages(case: Case, decision: Decision) -> list[dict]:
    """Converte a discussão em mensagens alternadas user/assistant (mensagens seguidas da equipe são unidas)."""
    opening = _decision_opening(case, decision)
    msgs: list[dict] = [{"role": "user", "content": opening}]
    for m in decision.messages:
        if m.role == "assistant":
            text = m.content
            role = "assistant"
        else:
            autor = m.author.name if m.author else "Equipe"
            text = f"[{autor}]: {m.content}"
            role = "user"
        if msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + text
        else:
            msgs.append({"role": role, "content": text})
    if msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "Continue a análise considerando o que já foi discutido."})
    if len(msgs) == 1:
        msgs[0]["content"] += "\n\nAnalise este ponto de decisão: quais são as opções, os riscos e a sua recomendação?"
    return msgs


def stream_discussion(case: Case, decision: Decision) -> Iterator[str]:
    client = _client()
    with client.beta.messages.stream(
        model=settings.claude_model,
        max_tokens=64000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=build_messages(case, decision),
        thinking={"type": "adaptive"},
        output_config={"effort": settings.claude_effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        for text in stream.text_stream:
            yield text
        final = stream.get_final_message()
    if final.stop_reason == "refusal":
        yield "\n\n[O assistente recusou esta solicitação. Reformule a pergunta ou decida sem o apoio do assistente.]"
    elif final.stop_reason == "max_tokens":
        yield "\n\n[Resposta interrompida por limite de tamanho.]"


def _structured(prompt: str, schema: dict, max_tokens: int = 16000) -> dict:
    client = _client()
    response = client.beta.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
        thinking={"type": "adaptive"},
        output_config={"effort": settings.claude_effort, "format": {"type": "json_schema", "schema": schema}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise AssistantUnavailable("O assistente recusou a solicitação")
    if response.stop_reason == "max_tokens":
        raise AssistantUnavailable("Resposta do assistente truncada")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise AssistantUnavailable("Resposta sem conteúdo")
    return json.loads(text)


STEPS_SCHEMA = {
    "type": "object",
    "properties": {
        "passos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "descricao": {"type": "string"},
                    "prazo": {"type": "string", "description": "AAAA-MM-DD, ou vazio se não houver prazo determinável"},
                    "prazo_fatal": {"type": "boolean"},
                    "justificativa": {"type": "string"},
                },
                "required": ["descricao", "prazo", "prazo_fatal", "justificativa"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["passos"],
    "additionalProperties": False,
}


def suggest_steps(case: Case, decision: Decision) -> list[dict]:
    discussion = "\n\n".join(
        f"[{'Assistente' if m.role == 'assistant' else (m.author.name if m.author else 'Equipe')}]: {m.content}"
        for m in decision.messages
    )
    prompt = (
        f"{_decision_opening(case, decision)}\n\n"
        f"Decisão tomada: {decision.decisao or '(ainda não registrada)'}\n"
        f"Fundamentos: {decision.fundamentos or '—'}\n\n"
        f"Discussão:\n{discussion or '(sem discussão)'}\n\n"
        "Converta a decisão em próximos passos concretos e executáveis (quem faz o quê). Preencha 'prazo' apenas "
        "quando ele puder ser determinado a partir do registro; na dúvida, deixe vazio e explique na justificativa "
        "o que precisa ser conferido. Não repita passos que já estão pendentes."
    )
    return _structured(prompt, STEPS_SCHEMA)["passos"]


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "panorama": {"type": "string"},
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "movement_id": {"type": "integer"},
                    "resumo": {"type": "string"},
                    "impacto": {"type": "string", "enum": ["alto", "medio", "baixo"]},
                    "exige_decisao": {"type": "boolean"},
                    "titulo_decisao": {"type": "string"},
                    "relacao_estrategia": {"type": "string"},
                },
                "required": ["movement_id", "resumo", "impacto", "exige_decisao", "titulo_decisao", "relacao_estrategia"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["panorama", "itens"],
    "additionalProperties": False,
}


def triage_movements(case: Case, movements: list[Movement]) -> dict:
    novos = "\n".join(
        f"- id={m.id} | {m.data.strftime('%d/%m/%Y')} [{m.fonte}] {m.titulo}\n  {_clip(m.texto, 8000)}" for m in movements
    )
    prompt = (
        "<registro_do_processo>\n" + build_case_context(case) + "\n</registro_do_processo>\n\n"
        "Novos andamentos ainda não analisados:\n" + novos + "\n\n"
        "Para cada andamento: resuma o que aconteceu, classifique o impacto para a estratégia, diga se exige uma "
        "decisão da equipe (e, se sim, um título curto para o ponto de decisão; senão, deixe o título vazio) e "
        "explique a relação com a estratégia de longo prazo. Movimentos burocráticos (juntada, conclusão, "
        "remessa) normalmente têm impacto baixo e não exigem decisão. No panorama, diga em poucas linhas se a "
        "estratégia continua válida."
    )
    return _structured(prompt, TRIAGE_SCHEMA)
