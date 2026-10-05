"""Administração: equipe, endereços do PJe, trilha de auditoria e lixeira."""
import csv
import io
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .auth import admin_user, hash_password
from .db import get_db
from .models import (
    AuditLog, Case, Decision, Movement, NextStep, Note, PjeAviso, PjeCredential, PjeDocument, PjeEndpoint, User,
)
from .web import back, check_csrf, flash, get_or_404, parse_date, render

router = APIRouter()

TRASH = {"processo": Case, "andamento": Movement, "proximo_passo": NextStep, "decisao": Decision, "nota": Note}


# ------------------------------------------------------------------ equipe

@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.name)).all())


@router.post("/users")
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
    db.commit()
    return back("/users")


@router.post("/users/{uid}/toggle")
def users_toggle(uid: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    target = get_or_404(db, User, uid)
    if target.id != user.id:
        target.active = not target.active
        db.commit()
    return back("/users")


@router.post("/users/{uid}/reset")
def users_reset(uid: int, request: Request, csrf: str = Form(""), password: str = Form(...),
                db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    target = get_or_404(db, User, uid)
    if len(password) < 10:
        flash(request, "A senha deve ter ao menos 10 caracteres.")
    else:
        target.password_hash = hash_password(password)
        db.commit()
        flash(request, f"Senha de {target.name} redefinida.")
    return back("/users")


@router.post("/users/{uid}/admin")
def users_admin(uid: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    target = get_or_404(db, User, uid)
    if target.id != user.id:
        target.is_admin = not target.is_admin
        db.commit()
    return back("/users")


# ------------------------------------------------------------------ endereços MNI

@router.get("/admin/pje", response_class=HTMLResponse)
def endpoints_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    eps = db.scalars(select(PjeEndpoint).order_by(PjeEndpoint.tribunal, PjeEndpoint.instancia)).all()
    creds = db.scalars(select(PjeCredential)).all()
    return render(request, "admin_pje.html", user, endpoints=eps, creds=creds)


@router.post("/admin/pje")
def endpoints_create(request: Request, csrf: str = Form(""), tribunal: str = Form(...), instancia: str = Form("1g"),
                     url: str = Form(...), versao_mni: str = Form("2.2.2"), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    url = url.strip().removesuffix("?wsdl")
    if not url.startswith("https://"):
        flash(request, "O endereço do MNI deve começar com https:// (as credenciais trafegam na mensagem).")
        return back("/admin/pje")
    if versao_mni not in {"2.2.2", "2.2.3"}:
        flash(request, "Versão do MNI não suportada.")
        return back("/admin/pje")
    db.add(PjeEndpoint(tribunal=tribunal.strip().lower(), instancia=instancia.strip() or "1g", url=url, versao_mni=versao_mni))
    db.commit()
    return back("/admin/pje")


@router.post("/admin/pje/{ep_id}/edit")
def endpoints_edit(ep_id: int, request: Request, csrf: str = Form(""), url: str = Form(...), versao_mni: str = Form("2.2.2"),
                   portal_url: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    ep = get_or_404(db, PjeEndpoint, ep_id)
    url = url.strip().removesuffix("?wsdl")
    if not url.startswith("https://") or versao_mni not in {"2.2.2", "2.2.3"}:
        flash(request, "Endereço deve começar com https:// e a versão deve ser 2.2.2 ou 2.2.3.")
        return back("/admin/pje")
    if url != ep.url:
        ep.origem = f"Alterado manualmente por {user.name}"
        ep.last_check_at = ep.last_check_ok = ep.last_check_status = None
    ep.url, ep.versao_mni = url, versao_mni
    portal_url = portal_url.strip()
    ep.portal_url = portal_url if portal_url.startswith("https://") else None
    db.commit()
    return back("/admin/pje")


@router.post("/admin/pje/diagnosticar")
def endpoints_diagnose(request: Request, csrf: str = Form(""), ep_id: int | None = Form(None),
                       db: Session = Depends(get_db), user: User = Depends(admin_user)):
    """Testa os endereços buscando o WSDL — sem credenciais e sem consultar processos."""
    from . import pje_service

    check_csrf(request, csrf)
    eps = [get_or_404(db, PjeEndpoint, ep_id)] if ep_id else db.scalars(select(PjeEndpoint).where(PjeEndpoint.ativo.is_(True))).all()
    oks = sum(pje_service.diagnose_endpoint(db, ep).ok for ep in eps)
    db.commit()
    flash(request, f"Diagnóstico concluído: {oks} de {len(eps)} endereço(s) respondendo como MNI. Veja o resultado de cada um na tabela.")
    return back("/admin/pje")


@router.post("/admin/pje/presets")
def endpoints_presets(request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    from .pje_presets import seed_presets

    check_csrf(request, csrf)
    n = seed_presets(db)
    db.commit()
    flash(request, f"{n} endereço(s) pré-cadastrado(s) incluído(s)." if n else "Os endereços do TJRO e do TRF1 já estão cadastrados.")
    return back("/admin/pje")


@router.post("/admin/pje/{ep_id}/toggle")
def endpoints_toggle(ep_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    ep = get_or_404(db, PjeEndpoint, ep_id)
    ep.ativo = not ep.ativo
    db.commit()
    return back("/admin/pje")


# ------------------------------------------------------------------ auditoria

def _audit_query(user_id: str, case_id: str, tipo: str, inicio: str, fim: str, q: str):
    stmt = select(AuditLog)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == (None if user_id == "sistema" else int(user_id)))
    if case_id:
        stmt = stmt.where(AuditLog.case_id == int(case_id))
    if tipo:
        stmt = stmt.where(AuditLog.tipo == tipo)
    if d := parse_date(inicio):
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(d, datetime.min.time()))
    if d := parse_date(fim):
        stmt = stmt.where(AuditLog.created_at < datetime.combine(d + timedelta(days=1), datetime.min.time()))
    if q:
        stmt = stmt.where(AuditLog.acao.ilike(f"%{q}%"))
    return stmt.order_by(AuditLog.id.desc())


@router.get("/admin/auditoria", response_class=HTMLResponse)
def audit_page(request: Request, user_id: str = "", case_id: str = "", tipo: str = "", inicio: str = "", fim: str = "", q: str = "",
               page: int = 1, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    per = 100
    rows = db.scalars(_audit_query(user_id, case_id, tipo, inicio, fim, q).offset((page - 1) * per).limit(per + 1)).all()
    entries = [(r, json.loads(r.detalhes) if r.detalhes else None) for r in rows[:per]]
    users = db.scalars(select(User).order_by(User.name)).all()
    cases = db.scalars(select(Case).order_by(Case.titulo)).all()
    tipos = [t for t in db.scalars(select(AuditLog.tipo).distinct()) if t]
    return render(request, "admin_audit.html", user, entries=entries, users=users, cases=cases, tipos=sorted(tipos),
                  f={"user_id": user_id, "case_id": case_id, "tipo": tipo, "inicio": inicio, "fim": fim, "q": q},
                  page=page, has_next=len(rows) > per, integrity=request.session.pop("integrity", None))


@router.get("/admin/auditoria.csv")
def audit_csv(user_id: str = "", case_id: str = "", tipo: str = "", inicio: str = "", fim: str = "", q: str = "",
              db: Session = Depends(get_db), user: User = Depends(admin_user)):
    rows = db.scalars(_audit_query(user_id, case_id, tipo, inicio, fim, q)).all()
    audit.log(db, f"Exportou a trilha de auditoria em CSV ({len(rows)} registros)", tipo="acesso")
    db.commit()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "data_hora", "usuario", "ip", "processo_id", "tipo", "entidade", "entidade_id", "acao", "detalhes", "hash"])
    for r in rows:
        w.writerow([r.id, r.created_at.isoformat(sep=" ", timespec="seconds"), r.user_name or "Sistema", r.ip or "", r.case_id or "",
                    r.tipo or "", r.entidade or "", r.entidade_id or "", r.acao, r.detalhes or "", r.hash or ""])
    data = "﻿" + buf.getvalue()  # BOM para o Excel reconhecer UTF-8
    name = f"auditoria_{datetime.now():%Y%m%d_%H%M}.csv"
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/admin/auditoria/verificar")
def audit_verify(request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    ok, bad_id, total = audit.verify_chain(db)
    request.session["integrity"] = (
        f"Cadeia íntegra: {total} registros verificados." if ok
        else f"ATENÇÃO: inconsistência no registro #{bad_id}. A trilha pode ter sido alterada diretamente no banco."
    )
    audit.log(db, f"Verificou a integridade da auditoria: {'íntegra' if ok else f'inconsistência no #{bad_id}'}", tipo="acesso")
    db.commit()
    return back("/admin/auditoria")


# ------------------------------------------------------------------ lixeira

@router.get("/admin/lixeira", response_class=HTMLResponse)
def trash_page(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    items = []
    names = {u.id: u.name for u in db.scalars(select(User))}
    for kind, model in TRASH.items():
        for obj in db.scalars(select(model).where(model.deleted_at.is_not(None)).order_by(model.deleted_at.desc())):
            label = audit.LABELS[kind](obj)
            case = obj if isinstance(obj, Case) else obj.case
            items.append({"kind": kind, "id": obj.id, "label": label, "case": case, "deleted_at": obj.deleted_at,
                          "by": names.get(obj.deleted_by_id, "—")})
    items.sort(key=lambda i: i["deleted_at"], reverse=True)
    return render(request, "admin_trash.html", user, items=items)


def _trash_obj(db: Session, kind: str, obj_id: int):
    model = TRASH.get(kind)
    if model is None:
        raise HTTPException(404)
    return get_or_404(db, model, obj_id, allow_deleted=True)


@router.post("/admin/lixeira/{kind}/{obj_id}/restaurar")
def trash_restore(kind: str, obj_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(admin_user)):
    check_csrf(request, csrf)
    obj = _trash_obj(db, kind, obj_id)
    obj.deleted_at, obj.deleted_by_id = None, None
    db.commit()
    flash(request, "Registro restaurado.")
    return back("/admin/lixeira")


@router.post("/admin/lixeira/{kind}/{obj_id}/expurgar")
def trash_purge(kind: str, obj_id: int, request: Request, csrf: str = Form(""), confirmacao: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(admin_user)):
    """Exclusão definitiva. O conteúdo apagado fica preservado na trilha de auditoria."""
    check_csrf(request, csrf)
    obj = _trash_obj(db, kind, obj_id)
    if obj.deleted_at is None:
        raise HTTPException(400, "Só é possível expurgar itens que estão na lixeira")
    if confirmacao.strip().upper() != "EXPURGAR":
        flash(request, "Digite EXPURGAR para confirmar a exclusão definitiva.")
        return back("/admin/lixeira")
    if isinstance(obj, Case):
        for m in (PjeAviso, PjeDocument):
            for row in db.scalars(select(m).where(m.case_id == obj.id)):
                row.case_id = None
        # registra o conteúdo dos itens filhos antes de apagá-los em cascata
        for child in [*obj.all_movements, *obj.all_steps, *obj.all_decisions, *obj.all_notes]:
            db.delete(child)
    db.delete(obj)
    db.commit()
    flash(request, "Registro apagado definitivamente. Seu conteúdo permanece na trilha de auditoria.")
    return back("/admin/lixeira")
