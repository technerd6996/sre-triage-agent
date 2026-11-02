from app.ports import registry
from app.adapters.cmdbhosts.hosts import MANIFEST, CmdbHosts

registry.register(MANIFEST, CmdbHosts)