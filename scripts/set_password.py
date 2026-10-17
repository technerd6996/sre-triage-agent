"""Create or reset a user. Run from the project root with the venv active:
   python -m scripts.set_password EMAIL PASSWORD [role] [name]
   role: viewer (end user) | approver | admin   (default: keeps the existing role, or viewer for a new user)
"""
import sys
from passlib.context import CryptContext
from app.store import supabase

if len(sys.argv) < 3:
    sys.exit(__doc__)

email, password = sys.argv[1].strip().lower(), sys.argv[2]
role = sys.argv[3] if len(sys.argv) > 3 else None
name = sys.argv[4] if len(sys.argv) > 4 else email.split("@")[0]
if role and role not in ("viewer", "approver", "admin"):
    sys.exit("role must be viewer, approver or admin")

h = CryptContext(schemes=["bcrypt"]).hash(password)
existing = supabase.table("users").select("user_id").eq("user_id", email).execute().data

if existing:
    patch = {"password_hash": h, "status": "active"}
    if role:
        patch["role"] = role
    supabase.table("users").update(patch).eq("user_id", email).execute()
    print(f"Updated {email}: password reset, status active" + (f", role {role}" if role else ""))
else:
    supabase.table("users").insert({
        "user_id": email, "name": name, "role": role or "viewer",
        "type": "human", "status": "active", "password_hash": h}).execute()
    print(f"Created {email} as {role or 'viewer'}")