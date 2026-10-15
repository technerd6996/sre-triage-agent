"""Run from the project root:
  python -m scripts.test_infra                      reachability + disk, both servers
  python -m scripts.test_infra --ad some.username   also look up an AD user
  python -m scripts.test_infra --write              also clear temp files (changes the servers)
"""
import sys

from dotenv import load_dotenv

load_dotenv()

from app.tools.infra import check_disk_usage, check_reachability, clear_temp_files, lookup_ad_user

args = sys.argv[1:]

for target in ("windows", "linux"):
    print(f"\n== {target} ==")
    print("reachability:", check_reachability.invoke({"target": target}))
    print("disk usage:  ", check_disk_usage.invoke({"target": target}))
    if "--write" in args:
        print("clear temp:  ", clear_temp_files.invoke({"target": target}))

if "--ad" in args:
    user = args[args.index("--ad") + 1]
    print("\n== AD lookup ==")
    print(lookup_ad_user.invoke({"username": user}))