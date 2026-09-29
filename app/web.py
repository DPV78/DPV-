"""Utilitários compartilhados pelas rotas web."""
import secrets
from datetime import date, datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session

from .models import User

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _fmt_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime) and (value.hour or value.minute):
        return value.strftime("%d/%m/%Y %H:%M")
    return value.strftime("%d/%m/%Y")


templates.env.filters["dt"] = _fmt_date
templates.env.globals["today"] = date.today
# Logotipo em vetor (extraído do Manual de Marca), inline para herdar a cor via CSS
_LOGO = (BASE_DIR / "static" / "logo.svg").read_text(encoding="utf-8")
templates.env.globals["brand_font"] = (BASE_DIR / "static" / "fonts" / "BostonAngel-Bold.woff2").exists()
templates.env.globals["logo_svg"] = Markup(_LOGO.replace("<svg ", '<svg class="logo" ', 1))


def render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    flash_msg = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {"user": user, "flash": flash_msg, "csrf": request.session["csrf"], **ctx})


def check_csrf(request: Request, token: str) -> None:
    if not token or token != request.session.get("csrf"):
        raise HTTPException(400, "Token de formulário inválido. Recarregue a página.")


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message[:600]


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def get_or_404(db: Session, model, obj_id: int, *, allow_deleted: bool = False):
    obj = db.get(model, obj_id)
    if obj is None or (not allow_deleted and getattr(obj, "deleted_at", None) is not None):
        raise HTTPException(404, "Registro não encontrado")
    return obj


def soft_delete(obj, user: User) -> None:
    obj.deleted_at = datetime.now()
    obj.deleted_by_id = user.id
