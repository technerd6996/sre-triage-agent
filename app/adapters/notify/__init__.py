from app.ports import registry
from app.adapters.notify.senders import (SES_MANIFEST, SMTP_MANIFEST, SNS_MANIFEST,
                                         SesNotifier, SmtpNotifier, SnsNotifier)

registry.register(SMTP_MANIFEST, SmtpNotifier)
registry.register(SES_MANIFEST, SesNotifier)
registry.register(SNS_MANIFEST, SnsNotifier)