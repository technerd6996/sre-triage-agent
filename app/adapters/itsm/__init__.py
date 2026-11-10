"""Ticketing adapters other than ServiceNow. Which one is active comes from the licence key (see registry.active_provider)."""
from app.ports import registry
from app.adapters.itsm import bmc, freshservice, jira, manageengine

registry.register(jira.MANIFEST, jira.JiraTicketing)
registry.register(freshservice.MANIFEST, freshservice.FreshserviceTicketing)
registry.register(manageengine.MANIFEST, manageengine.ManageEngineTicketing)
registry.register(bmc.MANIFEST, bmc.BmcTicketing)
