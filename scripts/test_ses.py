"""Checks that the SES sender and permissions work, WITHOUT mailing any person: it sends one message to Amazon's mailbox
simulator (success@simulator.amazonses.com), which accepts it and delivers nothing.

Run from the repo root:   python -m scripts.test_ses
Passes only when (1) the IAM user may call ses:SendEmail and (2) the sender address (setting SES_FROM) is a verified identity
in the SES region. In the SES sandbox this is enough for the simulator, but real owner mailboxes must be verified too,
or the account must have production access."""
import os

from app import bootstrap_store

bootstrap_store.load()
from app import config  # noqa: E402
import app.owner_contacts  # noqa: E402,F401  (registers the owners.* settings)
from app.ports import registry  # noqa: E402

config.hydrate_environ()

sender, region = os.getenv("SES_FROM", ""), os.getenv("AWS_REGION", "")
print(f"sender: {sender or '(not set)'}   region: {region or '(default)'}   owner mail transport: {config.get('owners.mail_transport')}")
try:
    mid = registry.get("notifications", "ses").send(
        ["success@simulator.amazonses.com"], [], "[TRIAGE-1] SES test (simulator, not delivered)",
        "Test of the TRIAGE-1 owner mail path. Sent to the Amazon SES mailbox simulator, so no person receives it.")
    print(f"OK: SES accepted the message ({mid}). Owner mail can be sent from {sender}.")
except Exception as e:                                         # noqa: BLE001
    msg = str(e)
    print("FAILED:", msg[:300])
    if "not verified" in msg:
        print("-> Verify the sender in the SES console for this region (SES > Verified identities > Create identity), "
              "then click the link in the verification email, and run this again.")
    elif "AccessDenied" in msg or "not authorized" in msg:
        print("-> The IAM user needs permission for ses:SendEmail.")
    raise SystemExit(1)
