from app.ports import registry
from app.adapters.ad.identity import MANIFEST, ActiveDirectoryIdentity

registry.register(MANIFEST, ActiveDirectoryIdentity)