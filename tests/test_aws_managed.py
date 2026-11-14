"""Managed-server scope: only servers whose ServiceNow CMDB record has the managed-server TAG are seen, counted or
acted on. A ticket about any other server is escalated as 'not managed by TRIAGE-1 App'. No network."""
import pytest

from app import config, licence
from app.adapters.aws.hosts import AwsHosts
from app.ports.errors import NotFound, Unavailable
from app.tools import aws, cmdb_tags

TAG = "triage1:managed=true"


def _inst(iid, name, ip):
    return {"InstanceId": iid, "State": {"Name": "running"}, "PublicIpAddress": ip, "PrivateIpAddress": "10.0.0.1",
            "Tags": [{"Key": "Name", "Value": name}], "Platform": "windows"}


class FakeEC2:
    def __init__(self, instances):
        self.instances = instances

    def get_paginator(self, op):
        ec2 = self

        class P:
            def paginate(self, Filters):
                return [{"Reservations": [{"Instances": ec2.instances}]}]
        return P()

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [i for i in self.instances if i["InstanceId"] in InstanceIds]}]}


class FakeSnow:
    """label / label_entry / cmdb_ci the way the ServiceNow Table API returns them."""

    def __init__(self):
        self.labels = {"L1": TAG, "L2": "other-tag"}
        self.entries = [("L1", "ci-win"), ("L1", "ci-byname"), ("L2", "ci-app")]
        self.cis = {"ci-win": {"name": "term-win-srv", "asset_tag": "i-win"},
                    "ci-byname": {"name": "victim-test", "asset_tag": ""},
                    "ci-app": {"name": "triage1-app", "asset_tag": "i-app"}}
        self.calls, self.down = 0, False

    def _request(self, method, table, params=None, **kw):
        self.calls += 1
        if self.down:
            raise RuntimeError("ServiceNow is down")
        q = params["sysparm_query"]
        if table == "label_entry":
            name = q.removeprefix("label.name=")
            return [{"table_key": key} for lab, key in self.entries if self.labels[lab] == name]
        if table == "cmdb_ci":
            ids = q.removeprefix("sys_idIN").split(",")
            return [{"sys_id": k, **v} for k, v in self.cis.items() if k in ids]
        raise AssertionError(table)


@pytest.fixture
def lab(monkeypatch):
    rows = {}
    monkeypatch.setattr(config, "_rows", lambda: rows)
    snow = FakeSnow()
    monkeypatch.setattr(cmdb_tags, "_snow", lambda: snow)
    monkeypatch.setattr(cmdb_tags, "_CACHE", {"tag": None, "at": 0.0, "cis": None, "error": ""})
    ec2 = FakeEC2([_inst("i-app", "triage1-app", "1.2.3.4"), _inst("i-win", "renamed-in-ec2", "5.6.7.8"),
                   _inst("i-victim", "victim-test", "9.9.9.9")])
    monkeypatch.setattr(aws, "_ec2", lambda: ec2)
    monkeypatch.setattr(aws, "_CACHE", {"at": 0.0, "instances": []})
    snow.rows = rows
    return snow


def test_the_tagged_cis_are_read_from_servicenow(lab):
    assert sorted(c["name"] for c in cmdb_tags.tagged_cis()) == ["term-win-srv", "victim-test"]


def test_an_instance_matches_its_ci_by_asset_tag_or_by_name(lab):
    names = sorted(i["instance_id"] for i in aws.list_instances())
    assert names == ["i-victim", "i-win"]          # i-win by asset_tag (EC2 name differs), i-victim by name


def test_an_empty_tag_manages_everything(lab):
    lab.rows["cfg.cmdb.managed_tag"] = ""
    assert len(aws.list_instances()) == 3 and cmdb_tags.tagged_cis() is None and lab.calls == 0


def test_the_list_is_cached_and_a_changed_tag_applies_at_once(lab):
    cmdb_tags.tagged_cis()
    n = lab.calls
    cmdb_tags.tagged_cis()
    assert lab.calls == n                           # cached
    lab.rows["cfg.cmdb.managed_tag"] = "other-tag"
    assert [c["name"] for c in cmdb_tags.tagged_cis()] == ["triage1-app"]


def test_servicenow_down_uses_the_last_list(lab):
    first = cmdb_tags.tagged_cis()
    lab.down = True
    assert cmdb_tags.tagged_cis(force=True) == first


def test_servicenow_down_with_no_earlier_list_manages_nothing(lab):
    lab.down = True
    with pytest.raises(cmdb_tags.TagsUnreadable, match="could not read the ServiceNow tag"):
        cmdb_tags.tagged_cis()
    with pytest.raises(Unavailable):
        AwsHosts().list_hosts()


def test_an_untagged_instance_is_refused_even_by_id(lab):
    assert aws.get_instance("i-win")["instance_id"] == "i-win"
    with pytest.raises(aws.NotManaged, match="is not managed by TRIAGE-1 App"):
        aws.get_instance("i-app")
    with pytest.raises(NotFound, match="is not managed by TRIAGE-1 App"):
        AwsHosts().connect_address("i-app")


def test_ticket_text_only_matches_managed_servers_and_names_the_unmanaged_one(lab):
    assert aws.find_in_text("triage1-app is slow") is None
    assert aws.find_in_text("nginx down on victim-test")["instance_id"] == "i-victim"
    assert aws.find_unmanaged_in_text("triage1-app is slow")["instance_id"] == "i-app"
    assert aws.find_unmanaged_in_text("nginx down on victim-test") is None


def test_the_agent_escalates_an_unmanaged_server_as_not_managed(lab, monkeypatch):
    from app.agent import hosts
    from app.ports import registry
    monkeypatch.setattr(registry, "get_hosts", lambda: AwsHosts())
    inst, why = hosts.resolve_server("[Zabbix]: triage1-app - Windows: high memory")
    assert inst is None and "Server triage1-app is not managed by TRIAGE-1 App" in why and hosts.is_not_managed(why)
    inst, why = hosts.resolve_server("[Zabbix]: nobody-srv - Windows: high memory")
    assert inst is None and not hosts.is_not_managed(why)


def test_the_cmdb_provider_follows_the_same_tag(lab, monkeypatch):
    import app.adapters.cmdbhosts.hosts as ch
    rows = [{"sys_id": k, "name": v["name"], "sys_class_name": "cmdb_ci_win_server", "os": "Windows",
             "operational_status": "1", "ip_address": ""} for k, v in lab.cis.items()]
    monkeypatch.setattr(ch, "_rows", lambda force=False: rows)
    c = ch.CmdbHosts()
    assert sorted(h.name for h in c.list_hosts()) == ["term-win-srv", "victim-test"]
    assert c.find_in_text("triage1-app down") is None and c.find_unmanaged_in_text("triage1-app down").name == "triage1-app"
    with pytest.raises(NotFound, match="not managed by TRIAGE-1 App"):
        c.get_host("triage1-app")
    assert ch.CmdbHosts(managed_only=False).find_by_name("triage1-app").os == "windows"     # OS hint still works


def test_the_licence_counts_managed_servers_only(lab, monkeypatch):
    from app.ports import registry
    monkeypatch.setattr(registry, "get_hosts", lambda: AwsHosts())
    assert licence._count_servers() == 2 and licence._servers_label() == "managed servers"
    lab.rows["cfg.cmdb.managed_tag"] = ""
    assert licence._count_servers() == 3 and licence._servers_label() == "servers"


@pytest.mark.parametrize("bad", ["a^b", "a\nb", "x" * 101])
def test_a_bad_tag_is_refused(bad):
    with pytest.raises(ValueError, match="Managed-server tag"):
        config.validate("cmdb.managed_tag", bad)


def test_empty_is_allowed_and_warned_about_in_the_admin_panel():
    from app.api import config_pane
    assert config.validate("cmdb.managed_tag", "") == "" and config.validate("cmdb.managed_tag", TAG) == TAG
    assert "EVERY server is managed" in config_pane._warning({"key": "cmdb.managed_tag", "value": ""})
    assert config_pane._warning({"key": "cmdb.managed_tag", "value": TAG}) == ""
    assert "aws.managed_tag" not in config.SETTINGS


def test_count_errors_are_explained():
    assert "Clear 'AWS profile'" in licence._explain("servers", "The config profile () could not be found")
    assert "ec2:DescribeInstances" in licence._explain("servers", "An error occurred (UnauthorizedOperation) ...")
    assert "Managed-server tag" in licence._explain("servers", "could not read the ServiceNow tag 'x' from the CMDB")
    assert "e.g." not in licence._explain("servers", "You must specify a region.")
