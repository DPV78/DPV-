"""Carteira Estratégica — aplicação web compartilhada para a gestão dos processos estratégicos do escritório."""
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import ai, cnj
from .auth import LoginRequired, admin_user, current_user, hash_password, verify_password
from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .models import (
    AuditLog, Case, Decision, DiscussionMessage, Movement, NextStep, Note, StrategyVersion, User,
)
from .sync import sync_all, sync_case

log = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _fmt_date(value) -> str:
    if not value:
        return ""
    return value.strftime("%d/%m/%Y %H:%M" if isinstance(value, datetime) and (value.hour or value.minute) else "%d/%m/%Y")


templates.env.filters["dt"] = _fmt_date
templates.env.globals["today"] = date.today
# Logotipo em vetor (extraído do Manual de Marca), inline para herdar a cor via CSS
_LOGO = (BASE_DIR / "static" / "logo.svg").read_text(encoding="utf-8")
templates.env.globals["logo_svg"] = Markup(_LOGO.replace("<svg ", '<svg class="logo" ', 1))


def init_db() -> None:
    Base.metadata.create_all(engine)
    if settings.admin_email and settings.admin_password:
        with SessionLocal() as db:
            if not db.scalar(select(func.count(User.id))):
                db.add(User(name=settings.admin_name, email=settings.admin_email.lower(),
                            password_hash=hash_password(settings.admin_password), is_admin=True))
                db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = None
    if settings.sync_interval_hours > 0:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()
        scheduler.add_job(sync_all, "interval", hours=settings.sync_interval_hours, args=[SessionLocal],
                          id="sync_all", next_run_time=datetime.now() + timedelta(minutes=1))
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Carteira Estratégica", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax",
                   https_only=settings.secret_key != "dev-inseguro-troque", max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


def render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {"user": user, "flash": flash, "csrf": request.session["csrf"], **ctx})


def check_csrf(request: Request, token: str) -> None:
    if not token or token != request.session.get("csrf"):
        raise HTTPException(400, "Token de formulário inválido. Recarregue a página.")


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def audit(db: Session, user: User | None, case_id: int | None, acao: str) -> None:
    db.add(AuditLog(user_id=user.id if user else None, case_id=case_id, acao=acao[:300]))


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def get_case(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "Processo não encontrado")
    return case


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------- autenticação

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        flash(request, "E-mail ou senha inválidos.")
        return back("/login")
    request.session.clear()
    request.session["uid"] = user.id
    return back("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------- painel

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    cases = db.scalars(select(Case).order_by(Case.status, Case.prioridade, Case.titulo)).all()
    unread = dict(db.execute(select(Movement.case_id, func.count()).where(Movement.lido.is_(False)).group_by(Movement.case_id)).all())
    open_dec = dict(db.execute(select(Decision.case_id, func.count()).where(Decision.status == "aberta").group_by(Decision.case_id)).all())
    next_deadline = dict(db.execute(
        select(NextStep.case_id, func.min(NextStep.prazo)).where(NextStep.status == "pendente", NextStep.prazo.is_not(None)).group_by(NextStep.case_id)
    ).all())
    upcoming = db.scalars(
        select(NextStep).where(NextStep.status == "pendente", NextStep.prazo.is_not(None), NextStep.prazo <= date.today() + timedelta(days=15))
        .order_by(NextStep.prazo)
    ).all()
    recent = db.scalars(select(Movement).where(Movement.lido.is_(False)).order_by(Movement.data.desc()).limit(15)).all()
    return render(request, "dashboard.html", user, cases=cases, unread=unread, open_dec=open_dec,
                  next_deadline=next_deadline, upcoming=upcoming, recent=recent)


@app.get("/agenda", response_class=HTMLResponse)
def agenda(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), mine: int = 0):
    q = select(NextStep).where(NextStep.status == "pendente")
    if mine:
        q = q.where(NextStep.responsavel_id == user.id)
    steps = db.scalars(q.order_by(NextStep.prazo.is_(None), NextStep.prazo, NextStep.created_at)).all()
    return render(request, "agenda.html", user, steps=steps, mine=mine)


# ---------------------------------------------------------------- processos

@app.get("/cases/new", response_class=HTMLResponse)
def case_new(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all()
    return render(request, "case_form.html", user, case=None, users=users)


@app.post("/cases/new")
def case_create(
    request: Request, csrf: str = Form(""), numero_cnj: str = Form(...), tribunal: str = Form(""), titulo: str = Form(...),
    cliente: str = Form(...), polo: str = Form("ativo"), parte_contraria: str = Form(""), valor_causa: str = Form(""),
    fase: str = Form(""), prioridade: str = Form("alta"), responsavel_id: str = Form(""),
    objetivo: str = Form(""), estrategia: str = Form(""), riscos: str = Form(""), premissas: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    numero = cnj.format_cnj(numero_cnj)
    if not cnj.is_valid(numero):
        flash(request, f"Número CNJ inválido (dígito verificador não confere): {numero_cnj}")
        return back("/cases/new")
    if db.scalar(select(Case.id).where(Case.numero_cnj == numero)):
        flash(request, "Este processo já está na carteira.")
        return back("/cases/new")
    trib = (tribunal.strip().lower() or cnj.guess_tribunal(numero) or "").strip()
    if not trib:
        flash(request, "Informe o tribunal (alias do DataJud, ex.: tjsp, trf3, stj).")
        return back("/cases/new")
    case = Case(
        numero_cnj=numero, tribunal=trib, titulo=titulo.strip(), cliente=cliente.strip(), polo=polo,
        parte_contraria=parte_contraria or None, valor_causa=valor_causa or None, fase=fase or None,
        prioridade=prioridade, responsavel_id=int(responsavel_id) if responsavel_id else user.id,
        objetivo=objetivo or None, estrategia=estrategia or None, riscos=riscos or None, premissas=premissas or None,
    )
    db.add(case)
    db.flush()
    db.add(StrategyVersion(case_id=case.id, objetivo=case.objetivo, estrategia=case.estrategia, riscos=case.riscos,
                           premissas=case.premissas, motivo="Estratégia inicial", author_id=user.id))
    audit(db, user, case.id, "Processo cadastrado")
    db.commit()
    flash(request, "Processo cadastrado. Use \"Sincronizar agora\" para importar os andamentos.")
    return back(f"/cases/{case.id}")


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(case_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = get_case(db, case_id)
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all()
    logs = db.scalars(select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.created_at.desc()).limit(30)).all()
    return render(request, "case.html", user, case=case, users=users, logs=logs,
                  pending=sorted((s for s in case.steps if s.status == "pendente"), key=lambda s: (s.prazo is None, s.prazo or date.max)),
                  done=[s for s in case.steps if s.status != "pendente"])


@app.get("/cases/{case_id}/edit", response_class=HTMLResponse)
def case_edit_page(case_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all()
    return render(request, "case_form.html", user, case=get_case(db, case_id), users=users)


@app.post("/cases/{case_id}/edit")
def case_edit(
    case_id: int, request: Request, csrf: str = Form(""), tribunal: str = Form(...), titulo: str = Form(...),
    cliente: str = Form(...), polo: str = Form("ativo"), parte_contraria: str = Form(""), valor_causa: str = Form(""),
    fase: str = Form(""), prioridade: str = Form("alta"), status: str = Form("ativo"), responsavel_id: str = Form(""),
    orgao_julgador: str = Form(""), classe: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    case.tribunal, case.titulo, case.cliente, case.polo = tribunal.strip().lower(), titulo.strip(), cliente.strip(), polo
    case.parte_contraria, case.valor_causa, case.fase = parte_contraria or None, valor_causa or None, fase or None
    case.prioridade, case.status = prioridade, status
    case.orgao_julgador, case.classe = orgao_julgador or None, classe or None
    case.responsavel_id = int(responsavel_id) if responsavel_id else None
    audit(db, user, case.id, "Dados do processo atualizados")
    db.commit()
    return back(f"/cases/{case_id}")


@app.post("/cases/{case_id}/strategy")
def case_strategy(
    case_id: int, request: Request, csrf: str = Form(""), objetivo: str = Form(""), estrategia: str = Form(""),
    riscos: str = Form(""), premissas: str = Form(""), motivo: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    if not motivo.strip():
        flash(request, "Informe o motivo da revisão da estratégia.")
        return back(f"/cases/{case_id}#estrategia")
    case.objetivo, case.estrategia = objetivo or None, estrategia or None
    case.riscos, case.premissas = riscos or None, premissas or None
    db.add(StrategyVersion(case_id=case.id, objetivo=case.objetivo, estrategia=case.estrategia, riscos=case.riscos,
                           premissas=case.premissas, motivo=motivo.strip(), author_id=user.id))
    audit(db, user, case.id, f"Estratégia revisada: {motivo.strip()}")
    db.commit()
    flash(request, "Estratégia atualizada e versionada.")
    return back(f"/cases/{case_id}#estrategia")


@app.post("/cases/{case_id}/sync")
def case_sync(case_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    res = sync_case(db, case)
    total = res["novos"]["datajud"] + res["novos"]["djen"]
    msg = f"Sincronização concluída: {total} andamento(s) novo(s)."
    if res["erros"]:
        msg += " Avisos: " + " | ".join(res["erros"])
    flash(request, msg)
    return back(f"/cases/{case_id}#andamentos")


@app.post("/cases/{case_id}/triage")
def case_triage(case_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Assistente analisa os andamentos não lidos e abre pontos de decisão quando necessário."""
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    novos = [m for m in case.movements if not m.lido][:30]
    if not novos:
        flash(request, "Não há andamentos não lidos para analisar.")
        return back(f"/cases/{case_id}")
    try:
        result = ai.triage_movements(case, novos)
    except ai.AssistantUnavailable as e:
        flash(request, f"Assistente indisponível: {e}")
        return back(f"/cases/{case_id}")
    except Exception as e:  # erro de API: não derruba a página
        log.exception("Falha na triagem")
        flash(request, f"Falha ao consultar o assistente: {type(e).__name__}")
        return back(f"/cases/{case_id}")
    by_id = {m.id: m for m in novos}
    criadas = 0
    for item in result.get("itens", []):
        mov = by_id.get(item.get("movement_id"))
        if mov is None:
            continue
        mov.analise = f"{item.get('resumo', '')}\n\nRelação com a estratégia: {item.get('relacao_estrategia', '')}".strip()
        mov.impacto = item.get("impacto")
        if item.get("exige_decisao"):
            titulo = item.get("titulo_decisao") or mov.titulo
            ctx = f"Sugerido pelo assistente na triagem (impacto {item.get('impacto')}).\n{item.get('resumo')}\n\nRelação com a estratégia: {item.get('relacao_estrategia')}"
            db.add(Decision(case_id=case.id, titulo=titulo[:300], contexto=ctx, movement_id=mov.id, created_by_id=user.id))
            criadas += 1
    if result.get("panorama"):
        db.add(Note(case_id=case.id, texto=f"[Triagem do assistente] {result['panorama']}", author_id=None))
    audit(db, user, case.id, f"Triagem do assistente: {len(novos)} andamento(s), {criadas} ponto(s) de decisão aberto(s)")
    db.commit()
    flash(request, f"Triagem concluída: {len(novos)} andamento(s) analisado(s), {criadas} ponto(s) de decisão aberto(s). O panorama foi salvo nas notas.")
    return back(f"/cases/{case_id}#andamentos")


# ---------------------------------------------------------------- andamentos

@app.post("/cases/{case_id}/movements")
def movement_add(
    case_id: int, request: Request, csrf: str = Form(""), data: str = Form(...), titulo: str = Form(...), texto: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    d = parse_date(data) or date.today()
    db.add(Movement(case_id=case.id, fonte="manual", external_id=secrets.token_hex(8), data=datetime.combine(d, datetime.min.time()),
                    titulo=titulo.strip()[:500], texto=texto or None, lido=True, created_by_id=user.id))
    audit(db, user, case.id, f"Andamento manual: {titulo.strip()[:200]}")
    db.commit()
    return back(f"/cases/{case_id}#andamentos")


@app.post("/movements/{movement_id}/read")
def movement_read(movement_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    m = db.get(Movement, movement_id)
    if not m:
        raise HTTPException(404)
    m.lido = True
    db.commit()
    return back(f"/cases/{m.case_id}#andamentos")


@app.post("/cases/{case_id}/movements/read-all")
def movement_read_all(case_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    for m in case.movements:
        m.lido = True
    audit(db, user, case.id, "Andamentos marcados como lidos")
    db.commit()
    return back(f"/cases/{case_id}#andamentos")


# ---------------------------------------------------------------- próximos passos

@app.post("/cases/{case_id}/steps")
def step_add(
    case_id: int, request: Request, csrf: str = Form(""), descricao: str = Form(...), prazo: str = Form(""),
    prazo_fatal: str = Form(""), responsavel_id: str = Form(""), decision_id: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    db.add(NextStep(case_id=case.id, descricao=descricao.strip(), prazo=parse_date(prazo), prazo_fatal=bool(prazo_fatal),
                    responsavel_id=int(responsavel_id) if responsavel_id else None,
                    decision_id=int(decision_id) if decision_id else None, created_by_id=user.id))
    audit(db, user, case.id, f"Próximo passo: {descricao.strip()[:200]}")
    db.commit()
    return back(request.headers.get("referer") or f"/cases/{case_id}#passos")


@app.post("/steps/{step_id}/status")
def step_status(step_id: int, request: Request, csrf: str = Form(""), status: str = Form(...),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    step = db.get(NextStep, step_id)
    if not step or status not in {"pendente", "concluido", "cancelado"}:
        raise HTTPException(400)
    step.status = status
    step.done_at = datetime.now() if status != "pendente" else None
    audit(db, user, step.case_id, f"Passo {status}: {step.descricao[:200]}")
    db.commit()
    return back(request.headers.get("referer") or f"/cases/{step.case_id}#passos")


# ---------------------------------------------------------------- notas

@app.post("/cases/{case_id}/notes")
def note_add(case_id: int, request: Request, csrf: str = Form(""), texto: str = Form(...),
             db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    db.add(Note(case_id=case.id, texto=texto.strip(), author_id=user.id))
    db.commit()
    return back(f"/cases/{case_id}#notas")


# ---------------------------------------------------------------- decisões e discussão

@app.post("/cases/{case_id}/decisions")
def decision_add(
    case_id: int, request: Request, csrf: str = Form(""), titulo: str = Form(...), contexto: str = Form(""),
    movement_id: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    d = Decision(case_id=case.id, titulo=titulo.strip()[:300], contexto=contexto or None,
                 movement_id=int(movement_id) if movement_id else None, created_by_id=user.id)
    db.add(d)
    db.flush()
    audit(db, user, case.id, f"Ponto de decisão aberto: {d.titulo}")
    db.commit()
    return back(f"/decisions/{d.id}")


def get_decision(db: Session, decision_id: int) -> Decision:
    d = db.get(Decision, decision_id)
    if not d:
        raise HTTPException(404, "Decisão não encontrada")
    return d


@app.get("/decisions/{decision_id}", response_class=HTMLResponse)
def decision_page(decision_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    d = get_decision(db, decision_id)
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all()
    steps = db.scalars(select(NextStep).where(NextStep.decision_id == d.id)).all()
    suggestions = json.loads(d.sugestoes_json) if d.sugestoes_json else None
    return render(request, "decision.html", user, d=d, case=d.case, users=users, steps=steps, suggestions=suggestions,
                  ai_enabled=bool(settings.anthropic_api_key))


@app.post("/decisions/{decision_id}/messages")
def decision_message(decision_id: int, request: Request, csrf: str = Form(""), content: str = Form(...),
                     db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    d = get_decision(db, decision_id)
    db.add(DiscussionMessage(decision_id=d.id, role="user", author_id=user.id, content=content.strip()))
    db.commit()
    if request.headers.get("accept") == "application/json":
        return JSONResponse({"ok": True})
    return back(f"/decisions/{decision_id}")


@app.post("/decisions/{decision_id}/assistant")
def decision_assistant(decision_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, request.headers.get("x-csrf-token", ""))
    d = get_decision(db, decision_id)
    case = d.case
    try:
        chunks = ai.stream_discussion(case, d)
        first = next(chunks)  # falhas de configuração/API aparecem antes de abrir o streaming
    except StopIteration:
        first, chunks = "", iter(())
    except ai.AssistantUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        log.exception("Falha ao consultar o assistente")
        return JSONResponse({"error": f"Falha ao consultar o assistente: {type(e).__name__}"}, status_code=502)

    def generate():
        parts = [first]
        yield first
        try:
            for text in chunks:
                parts.append(text)
                yield text
        except Exception as e:
            log.exception("Streaming interrompido")
            msg = f"\n\n[Resposta interrompida: {type(e).__name__}]"
            parts.append(msg)
            yield msg
        finally:
            content = "".join(parts).strip()
            if content:
                with SessionLocal() as s:
                    s.add(DiscussionMessage(decision_id=decision_id, role="assistant", content=content))
                    s.commit()

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


@app.post("/decisions/{decision_id}/decide")
def decision_decide(
    decision_id: int, request: Request, csrf: str = Form(""), decisao: str = Form(...), fundamentos: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    d = get_decision(db, decision_id)
    d.decisao, d.fundamentos = decisao.strip(), fundamentos.strip() or None
    d.status, d.decided_by_id, d.decided_at = "decidida", user.id, datetime.now()
    audit(db, user, d.case_id, f"Decisão registrada: {d.titulo}")
    db.commit()
    flash(request, "Decisão registrada. Agora registre os próximos passos (ou peça sugestões ao assistente).")
    return back(f"/decisions/{decision_id}#passos")


@app.post("/decisions/{decision_id}/reopen")
def decision_reopen(decision_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    d = get_decision(db, decision_id)
    d.status = "aberta"
    audit(db, user, d.case_id, f"Decisão reaberta: {d.titulo}")
    db.commit()
    return back(f"/decisions/{decision_id}")


@app.post("/decisions/{decision_id}/suggest-steps")
def decision_suggest(decision_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    d = get_decision(db, decision_id)
    try:
        d.sugestoes_json = json.dumps(ai.suggest_steps(d.case, d), ensure_ascii=False)
        db.commit()
    except ai.AssistantUnavailable as e:
        flash(request, f"Assistente indisponível: {e}")
    except Exception as e:
        log.exception("Falha ao sugerir passos")
        flash(request, f"Falha ao consultar o assistente: {type(e).__name__}")
    return back(f"/decisions/{decision_id}#passos")


@app.post("/decisions/{decision_id}/steps")
async def decision_steps_bulk(decision_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    d = get_decision(db, decision_id)
    count = 0
    for idx in form.getlist("sel"):
        desc = str(form.get(f"descricao_{idx}", "")).strip()
        if not desc:
            continue
        db.add(NextStep(case_id=d.case_id, decision_id=d.id, descricao=desc, prazo=parse_date(str(form.get(f"prazo_{idx}", ""))),
                        prazo_fatal=bool(form.get(f"fatal_{idx}")), created_by_id=user.id,
                        responsavel_id=int(str(form.get(f"resp_{idx}"))) if form.get(f"resp_{idx}") else None))
        count += 1
    d.sugestoes_json = None
    audit(db, user, d.case_id, f"{count} próximo(s) passo(s) criados a partir da decisão \"{d.titulo}\"")
    db.commit()
    flash(request, f"{count} passo(s) adicionados.")
    return back(f"/decisions/{decision_id}#passos")


# ---------------------------------------------------------------- equipe

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.name)).all())


@app.post("/users")
def users_create(
    request: Request, csrf: str = Form(""), name: str = Form(...), email: str = Form(...), oab: str = Form(""),
    password: str = Form(...), is_admin: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user),
):
    check_csrf(request, csrf)
    if len(password) < 10:
        flash(request, "A senha deve ter ao menos 10 caracteres.")
        return back("/users")
    if db.scalar(select(User.id).where(User.email == email.strip().lower())):
        flash(request, "Já existe um usuário com este e-mail.")
        return back("/users")
    db.add(User(name=name.strip(), email=email.strip().lower(), oab=oab or None, password_hash=hash_password(password), is_admin=bool(is_admin)))
    audit(db, user, None, f"Usuário criado: {email.strip().lower()}")
    db.commit()
    return back("/users")


@app.post("/users/{uid}/toggle")
def users_toggle(uid: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    target = db.get(User, uid)
    if target and target.id != user.id:
        target.active = not target.active
        audit(db, user, None, f"Usuário {'ativado' if target.active else 'desativado'}: {target.email}")
        db.commit()
    return back("/users")


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account.html", user)


@app.post("/account/password")
def change_password(request: Request, csrf: str = Form(""), atual: str = Form(...), nova: str = Form(...),
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    if not verify_password(atual, user.password_hash) or len(nova) < 10:
        flash(request, "Senha atual incorreta ou nova senha com menos de 10 caracteres.")
    else:
        user.password_hash = hash_password(nova)
        db.commit()
        flash(request, "Senha alterada.")
    return back("/account")
