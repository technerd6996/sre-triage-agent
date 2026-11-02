from app.ports import registry
from app.adapters.awssm.credentials import MANIFEST, AwsSecretsManager

registry.register(MANIFEST, AwsSecretsManager)