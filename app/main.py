"""Carteira Estratégica — aplicação web compartilhada para a gestão dos processos estratégicos do escritório."""
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import ai, audit, cnj
from .auth import LoginRequired, current_user, hash_password, verify_password
from .config import settings
from .db import SessionLocal, get_db
from .migrate import init_db
from .models import (
    Case, Decision, DiscussionMessage, Movement, NextStep, Note, PjeAviso, StrategyVersion, User,
)
from .sync import check_avisos_job, sync_all, sync_case
from .web import BASE_DIR, back, check_csrf, flash, get_or_404, parse_date, render, soft_delete

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = None
    if settings.sync_interval_hours > 0 or settings.avisos_interval_hours > 0 or settings.database_url.startswith("sqlite"):
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()
        if settings.sync_interval_hours > 0:
            scheduler.add_job(sync_all, "interval", hours=settings.sync_interval_hours, args=[SessionLocal],
                              id="sync_all", next_run_time=datetime.now() + timedelta(minutes=1))
        from .backup import run_backup

        scheduler.add_job(run_backup, "cron", hour=23, minute=0, id="backup")
        if settings.avisos_interval_hours > 0:
            scheduler.add_job(check_avisos_job, "interval", hours=settings.avisos_interval_hours, args=[SessionLocal],
                              id="avisos", next_run_time=datetime.now() + timedelta(minutes=2))
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Carteira Estratégica", lifespan=lifespan)


@app.middleware("http")
async def actor_context(request: Request, call_next):
    """Identifica quem está agindo (usuário e IP) para a trilha de auditoria."""
    session = request.scope.get("session") or {}
    ip = request.client.host if request.client else None
    token = audit.current_actor.set(audit.Actor(user_id=session.get("uid"), user_name=session.get("uname"), ip=ip))
    try:
        return await call_next(request)
    finally:
        audit.current_actor.reset(token)


# Adicionado depois = executa antes: a sessão precisa estar disponível para o middleware acima
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax",
                   https_only=settings.session_https_only, max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

from . import routes_admin, routes_pje  # noqa: E402

app.include_router(routes_pje.router)
app.include_router(routes_admin.router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return back("/login")


def active_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)))


def get_case(db: Session, case_id: int) -> Case:
    return get_or_404(db, Case, case_id)


# ---------------------------------------------------------------- autenticação

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        audit.log(db, f"Tentativa de login recusada: {email.strip().lower()[:100]}", tipo="acesso")
        db.commit()
        flash(request, "E-mail ou senha inválidos.")
        return back("/login")
    request.session.clear()
    request.session["uid"], request.session["uname"] = user.id, user.name
    audit.current_actor.set(audit.Actor(user.id, user.name, request.client.host if request.client else None))
    audit.log(db, "Login", tipo="acesso")
    db.commit()
    return back("/")


@app.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    if request.session.get("uid"):
        audit.log(db, "Logout", tipo="acesso")
        db.commit()
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------- painel

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    cases = db.scalars(select(Case).where(Case.deleted_at.is_(None)).order_by(Case.status, Case.prioridade, Case.titulo)).all()
    live = Movement.deleted_at.is_(None)
    unread = dict(db.execute(select(Movement.case_id, func.count()).where(Movement.lido.is_(False), live).group_by(Movement.case_id)).all())
    open_dec = dict(db.execute(select(Decision.case_id, func.count()).where(Decision.status == "aberta", Decision.deleted_at.is_(None)).group_by(Decision.case_id)).all())
    step_live = (NextStep.status == "pendente", NextStep.deleted_at.is_(None))
    next_deadline = dict(db.execute(
        select(NextStep.case_id, func.min(NextStep.prazo)).where(*step_live, NextStep.prazo.is_not(None)).group_by(NextStep.case_id)
    ).all())
    upcoming = db.scalars(
        select(NextStep).join(Case).where(*step_live, Case.deleted_at.is_(None), NextStep.prazo.is_not(None),
                                          NextStep.prazo <= date.today() + timedelta(days=15)).order_by(NextStep.prazo)
    ).all()
    recent = db.scalars(select(Movement).join(Case).where(Movement.lido.is_(False), live, Case.deleted_at.is_(None))
                        .order_by(Movement.data.desc()).limit(15)).all()
    avisos = db.scalars(select(PjeAviso).where(PjeAviso.status == "pendente").order_by(PjeAviso.data_disponibilizacao)).all()
    return render(request, "dashboard.html", user, cases=cases, unread=unread, open_dec=open_dec,
                  next_deadline=next_deadline, upcoming=upcoming, recent=recent, avisos=avisos)


@app.get("/agenda", response_class=HTMLResponse)
def agenda(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), mine: int = 0):
    q = select(NextStep).join(Case).where(NextStep.status == "pendente", NextStep.deleted_at.is_(None), Case.deleted_at.is_(None))
    if mine:
        q = q.where(NextStep.responsavel_id == user.id)
    steps = db.scalars(q.order_by(NextStep.prazo.is_(None), NextStep.prazo, NextStep.created_at)).all()
    return render(request, "agenda.html", user, steps=steps, mine=mine)


# ---------------------------------------------------------------- processos

@app.get("/cases/new", response_class=HTMLResponse)
def case_new(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "case_form.html", user, case=None, users=active_users(db))


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
    existing = db.scalar(select(Case).where(Case.numero_cnj == numero))
    if existing is not None:
        flash(request, "Este processo já está na carteira." if existing.deleted_at is None
              else "Este processo está na lixeira. Peça ao administrador para restaurá-lo.")
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
    db.commit()
    flash(request, "Processo cadastrado. Use \"Sincronizar agora\" para importar os andamentos.")
    return back(f"/cases/{case.id}")


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(case_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = get_case(db, case_id)
    from .models import AuditLog, PjeDocument

    logs = db.scalars(select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.id.desc()).limit(40)).all()
    avisos = db.scalars(select(PjeAviso).where(PjeAviso.case_id == case_id).order_by(PjeAviso.first_seen_at.desc())).all()
    docs = db.scalars(select(PjeDocument).where(PjeDocument.case_id == case_id).order_by(PjeDocument.created_at.desc())).all()
    pending = sorted((s for s in case.steps if s.status == "pendente"), key=lambda s: (s.prazo is None, s.prazo or date.max))
    from .pje_service import endpoints_for

    return render(request, "case.html", user, case=case, users=active_users(db), logs=logs, avisos=avisos, docs=docs,
                  pje_endpoints=endpoints_for(db, case.tribunal),
                  pending=pending, done=[s for s in case.steps if s.status != "pendente"])


@app.get("/cases/{case_id}/edit", response_class=HTMLResponse)
def case_edit_page(case_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "case_form.html", user, case=get_case(db, case_id), users=active_users(db))


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
    db.commit()
    flash(request, "Dados do processo atualizados.")
    return back(f"/cases/{case_id}")


@app.post("/cases/{case_id}/delete")
def case_delete(case_id: int, request: Request, csrf: str = Form(""), motivo: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    if not motivo.strip():
        flash(request, "Informe o motivo da exclusão.")
        return back(f"/cases/{case_id}/edit")
    soft_delete(case, user)
    audit.log(db, f"Motivo da exclusão do processo {case.numero_cnj}: {motivo.strip()}", tipo="excluir", case_id=case.id)
    db.commit()
    flash(request, f"Processo {case.numero_cnj} movido para a lixeira. Somente o administrador pode restaurá-lo ou apagá-lo definitivamente.")
    return back("/")


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
    db.commit()
    flash(request, "Estratégia atualizada e versionada.")
    return back(f"/cases/{case_id}#estrategia")


@app.post("/cases/{case_id}/sync")
def case_sync(case_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    res = sync_case(db, get_case(db, case_id))
    msg = f"Sincronização concluída: {sum(res['novos'].values())} andamento(s) novo(s)."
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
    db.commit()
    flash(request, f"Triagem concluída: {len(novos)} andamento(s) analisado(s), {criadas} ponto(s) de decisão aberto(s). O panorama foi salvo nas notas.")
    return back(f"/cases/{case_id}#andamentos")


# ---------------------------------------------------------------- andamentos

@app.post("/cases/{case_id}/movements")
def movement_add(
    case_id: int, request: Request, csrf: str = Form(""), data: str = Form(...), titulo: str = Form(...), texto: str = Form(""),
    link: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    d = parse_date(data) or date.today()
    db.add(Movement(case_id=case.id, fonte="manual", external_id=secrets.token_hex(8), data=datetime.combine(d, datetime.min.time()),
                    titulo=titulo.strip()[:500], texto=texto or None, link=link or None, lido=True, created_by_id=user.id))
    db.commit()
    return back(f"/cases/{case_id}#andamentos")


@app.get("/movements/{movement_id}/edit", response_class=HTMLResponse)
def movement_edit_page(movement_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = get_or_404(db, Movement, movement_id)
    return render(request, "movement_edit.html", user, m=m, case=m.case)


@app.post("/movements/{movement_id}/edit")
def movement_edit(
    movement_id: int, request: Request, csrf: str = Form(""), data: str = Form(...), titulo: str = Form(...),
    texto: str = Form(""), link: str = Form(""), impacto: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    m = get_or_404(db, Movement, movement_id)
    d = parse_date(data)
    if d and d != m.data.date():
        m.data = datetime.combine(d, m.data.time())
    m.titulo, m.texto, m.link = titulo.strip()[:500], texto or None, link or None
    m.impacto = impacto or None
    if m.fonte != "manual":
        m.editado = True
    db.commit()
    flash(request, "Andamento atualizado.")
    return back(f"/cases/{m.case_id}#andamentos")


@app.post("/movements/{movement_id}/delete")
def movement_delete(movement_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    m = get_or_404(db, Movement, movement_id)
    soft_delete(m, user)
    db.commit()
    flash(request, "Andamento movido para a lixeira (não será reimportado pela sincronização).")
    return back(f"/cases/{m.case_id}#andamentos")


@app.post("/movements/{movement_id}/read")
def movement_read(movement_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    m = get_or_404(db, Movement, movement_id)
    m.lido = True
    db.commit()
    return back(f"/cases/{m.case_id}#andamentos")


@app.post("/cases/{case_id}/movements/read-all")
def movement_read_all(case_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    for m in case.movements:
        m.lido = True
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
    db.commit()
    return back(request.headers.get("referer") or f"/cases/{case_id}#passos")


@app.get("/steps/{step_id}/edit", response_class=HTMLResponse)
def step_edit_page(step_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = get_or_404(db, NextStep, step_id)
    return render(request, "step_edit.html", user, s=s, case=s.case, users=active_users(db))


@app.post("/steps/{step_id}/edit")
def step_edit(
    step_id: int, request: Request, csrf: str = Form(""), descricao: str = Form(...), prazo: str = Form(""),
    prazo_fatal: str = Form(""), responsavel_id: str = Form(""), status: str = Form("pendente"),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    s = get_or_404(db, NextStep, step_id)
    if status not in {"pendente", "concluido", "cancelado"}:
        raise HTTPException(400)
    s.descricao, s.prazo, s.prazo_fatal = descricao.strip(), parse_date(prazo), bool(prazo_fatal)
    s.responsavel_id = int(responsavel_id) if responsavel_id else None
    if s.status != status:
        s.status = status
        s.done_at = datetime.now() if status != "pendente" else None
    db.commit()
    flash(request, "Próximo passo atualizado.")
    return back(f"/cases/{s.case_id}#passos")


@app.post("/steps/{step_id}/status")
def step_status(step_id: int, request: Request, csrf: str = Form(""), status: str = Form(...),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    step = get_or_404(db, NextStep, step_id)
    if status not in {"pendente", "concluido", "cancelado"}:
        raise HTTPException(400)
    step.status = status
    step.done_at = datetime.now() if status != "pendente" else None
    db.commit()
    return back(request.headers.get("referer") or f"/cases/{step.case_id}#passos")


@app.post("/steps/{step_id}/delete")
def step_delete(step_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    s = get_or_404(db, NextStep, step_id)
    soft_delete(s, user)
    db.commit()
    flash(request, "Próximo passo movido para a lixeira.")
    ref = request.headers.get("referer") or ""
    return back(ref if ref and "/steps/" not in ref else f"/cases/{s.case_id}#passos")


# ---------------------------------------------------------------- notas

@app.post("/cases/{case_id}/notes")
def note_add(case_id: int, request: Request, csrf: str = Form(""), texto: str = Form(...),
             db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    case = get_case(db, case_id)
    db.add(Note(case_id=case.id, texto=texto.strip(), author_id=user.id))
    db.commit()
    return back(f"/cases/{case_id}#notas")


@app.post("/notes/{note_id}/edit")
def note_edit(note_id: int, request: Request, csrf: str = Form(""), texto: str = Form(...),
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    n = get_or_404(db, Note, note_id)
    n.texto = texto.strip()
    db.commit()
    return back(f"/cases/{n.case_id}#notas")


@app.post("/notes/{note_id}/delete")
def note_delete(note_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    n = get_or_404(db, Note, note_id)
    soft_delete(n, user)
    db.commit()
    return back(f"/cases/{n.case_id}#notas")


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
    db.commit()
    return back(f"/decisions/{d.id}")


def get_decision(db: Session, decision_id: int) -> Decision:
    d = get_or_404(db, Decision, decision_id)
    if d.case.deleted_at is not None:
        raise HTTPException(404, "Processo na lixeira")
    return d


@app.get("/decisions/{decision_id}", response_class=HTMLResponse)
def decision_page(decision_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    d = get_decision(db, decision_id)
    steps = db.scalars(select(NextStep).where(NextStep.decision_id == d.id, NextStep.deleted_at.is_(None))).all()
    suggestions = json.loads(d.sugestoes_json) if d.sugestoes_json else None
    return render(request, "decision.html", user, d=d, case=d.case, users=active_users(db), steps=steps, suggestions=suggestions,
                  ai_enabled=bool(settings.anthropic_api_key))


@app.post("/decisions/{decision_id}/edit")
def decision_edit(
    decision_id: int, request: Request, csrf: str = Form(""), titulo: str = Form(...), contexto: str = Form(""),
    decisao: str = Form(""), fundamentos: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user),
):
    check_csrf(request, csrf)
    d = get_decision(db, decision_id)
    d.titulo, d.contexto = titulo.strip()[:300], contexto or None
    if d.status == "decidida":
        d.decisao, d.fundamentos = decisao.strip() or d.decisao, fundamentos.strip() or None
    db.commit()
    flash(request, "Ponto de decisão atualizado.")
    return back(f"/decisions/{decision_id}")


@app.post("/decisions/{decision_id}/delete")
def decision_delete(decision_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    d = get_decision(db, decision_id)
    soft_delete(d, user)
    db.commit()
    flash(request, "Ponto de decisão movido para a lixeira.")
    return back(f"/cases/{d.case_id}#decisoes")


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
    try:
        chunks = ai.stream_discussion(d.case, d)
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
    db.commit()
    flash(request, "Decisão registrada. Agora registre os próximos passos (ou peça sugestões ao assistente).")
    return back(f"/decisions/{decision_id}#passos")


@app.post("/decisions/{decision_id}/reopen")
def decision_reopen(decision_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    d = get_decision(db, decision_id)
    d.status = "aberta"
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
    db.commit()
    flash(request, f"{count} passo(s) adicionados.")
    return back(f"/decisions/{decision_id}#passos")


# ---------------------------------------------------------------- conta

@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .models import PjeCredential, PjeEndpoint

    endpoints = db.scalars(select(PjeEndpoint).where(PjeEndpoint.ativo.is_(True)).order_by(PjeEndpoint.tribunal, PjeEndpoint.instancia)).all()
    creds = db.scalars(select(PjeCredential).where(PjeCredential.user_id == user.id)).all()
    return render(request, "account.html", user, endpoints=endpoints, creds=creds, crypto_ok=bool(settings.credentials_key))


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
