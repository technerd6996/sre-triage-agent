from fastapi import APIRouter, Request
from pydantic import BaseModel
from app import store

router = APIRouter()


class ChatSwitch(BaseModel):
    enabled: bool


@router.get("/api/chat/status")                      # any logged-in user: lets the UI know chat is off
def chat_status():
    return {"enabled": bool(store.get_setting("chat_enabled", True))}


@router.post("/api/admin/chat")                      # /api/admin/* is admin-only in required_role
def set_chat(req: ChatSwitch, request: Request):
    who = getattr(request.state, "user", "unknown")
    store.set_setting("chat_enabled", req.enabled, who)
    store.audit(None, None, who, "chat_on" if req.enabled else "chat_off")
    return {"enabled": req.enabled}

class Autonomy(BaseModel):
    mode: str


@router.get("/api/admin/autonomy")
def get_autonomy():
    return {"mode": store.get_setting("autonomy_mode", "shadow")}


@router.post("/api/admin/autonomy")
def set_autonomy(req: Autonomy, request: Request):
    from fastapi import HTTPException
    if req.mode not in ("off", "shadow", "on"):
        raise HTTPException(422, "mode must be off, shadow or on")
    who = getattr(request.state, "user", "unknown")
    store.set_setting("autonomy_mode", req.mode, who)
    store.audit(None, None, who, "autonomy_mode", req.mode)
    return {"mode": req.mode}

class P1Settings(BaseModel):
    bridge_link: str = ""
    to: list[str] = []
    cc: list[str] = []


@router.get("/api/admin/p1")
def get_p1():
    return {"bridge_link": store.get_setting("bridge_link", ""), "to": store.get_setting("p1_notice_to", []),
            "cc": store.get_setting("p1_notice_cc", [])}


@router.post("/api/admin/p1")
def set_p1(req: P1Settings, request: Request):
    from fastapi import HTTPException
    from app.escalation import EMAIL
    bad = [a for a in req.to + req.cc if not EMAIL.match(a)]
    if bad:
        raise HTTPException(422, f"Invalid email: {', '.join(bad)}")
    if req.bridge_link and not req.bridge_link.startswith("https://"):
        raise HTTPException(422, "Bridge link must start with https://")
    who = getattr(request.state, "user", "unknown")
    store.set_setting("bridge_link", req.bridge_link, who)
    store.set_setting("p1_notice_to", req.to, who)
    store.set_setting("p1_notice_cc", req.cc, who)
    store.audit(None, None, who, "p1_settings_changed")
    return {"ok": True}