from app.ports import registry
from app.adapters.cyberark.credentials import MANIFEST, CyberArkCredentials

registry.register(MANIFEST, CyberArkCredentials)
