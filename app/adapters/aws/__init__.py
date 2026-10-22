from app.ports import registry
from app.adapters.aws.hosts import MANIFEST, AwsHosts

registry.register(MANIFEST, AwsHosts)