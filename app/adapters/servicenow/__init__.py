from app.ports import registry
from app.adapters.servicenow.ticketing import MANIFEST, ServiceNowTicketing
from app.adapters.servicenow.context import MANIFEST as CONTEXT_MANIFEST, ServiceNowContext

registry.register(MANIFEST, ServiceNowTicketing)
registry.register(CONTEXT_MANIFEST, ServiceNowContext)