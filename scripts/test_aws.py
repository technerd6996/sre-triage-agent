"""Run from the project root:
  python -m scripts.test_aws                          lists your instances
  python -m scripts.test_aws "text naming a host"     also shows which instance the text resolves to"""
import sys

from dotenv import load_dotenv

load_dotenv()

from app.tools import aws

for inst in aws.list_instances(force=True):
    print(inst)

if len(sys.argv) > 1:
    print("\nmatch:", aws.find_in_text(sys.argv[1]))