"""First-run setup API. Open only until setup is complete, and every action needs the setup code."""
import importlib.util
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import bootstrap, bootstrap_store, config, store

router = APIRouter(prefix="/api/setup")
_attempts: list[float] = []


class Code(BaseModel):
    code: str


class Complete(BaseModel):
    code: str
    password: str = Field(default="", max_length=200)        # the first Super Admin's password, chosen here


def _guard(code: str) -> None:
    if bootstrap.is_installed():
        raise HTTPException(404, "Setup is already complete.")
    now = time.time()
    _attempts[:] = [t for t in _attempts if now - t < 300]
    if len(_attempts) >= 10:
        raise HTTPException(429, "Too many wrong codes. Wait a few minutes.")
    if not bootstrap.check_setup_code(code):
        _attempts.append(now)
        raise HTTPException(403, "Wrong setup code. It is printed in the server console at startup.")


def _admin_needed() -> bool:
    try:
        return bootstrap.admin_needed()
    except Exception:
        return True


@router.get("/status")
def status():
    return {"database": bootstrap.database_check(), "secret_key": config.secrets_ready(),
            "admin_needed": _admin_needed(), "admin_user": bootstrap.DEFAULT_ADMIN_ID, "min_password": config.get("security.min_password"),
            "cryptography_installed": importlib.util.find_spec("cryptography") is not None}


@router.post("/generate-key")
def generate_key(req: Code):
    _guard(req.code)
    if config.secrets_ready():
        raise HTTPException(409, "An encryption key is already configured.")
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise HTTPException(500, "The 'cryptography' package is not installed. Run: uv add cryptography")
    key = Fernet.generate_key().decode()
    saved = False
    try:
        bootstrap_store.save(TRIAGE_SECRET_KEY=key)            # also usable straight away, without a restart
        saved = True
    except Exception:
        os.environ["TRIAGE_SECRET_KEY"] = key
    store.audit(None, None, "setup", "secret_key_generated", "saved to bootstrap file" if saved else "shown once")
    return {"key": key, "saved_to_env_file": saved}


@router.post("/complete")
def complete(req: Complete):
    _guard(req.code)
    try:
        return bootstrap.complete_setup(req.password)
    except ValueError as e:
        raise HTTPException(409, str(e))