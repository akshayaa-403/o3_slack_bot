"""Delete intents from an Amazon Lex V2 bot locale by name.

Cleanup helper for the demo: removes the intents a /generate-intents run created
so you can test the flow again from a clean state. Dry-run by default — prints
what WOULD be deleted and touches nothing; add --apply to actually delete.

    # see what would be deleted (the 4 demo intents, from BOT_ID in .env)
    python scripts/delete_lex_intents.py

    # actually delete them
    python scripts/delete_lex_intents.py --apply

    # delete specific intents by name
    python scripts/delete_lex_intents.py --names FooIntent BarIntent --apply

Reads BOT_ID, AWS_REGION, LOCALE_ID from .env (override with flags). AWS creds
come from your normal AWS CLI/environment setup.
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import load_lex_intents_from_excel as loader

# The 4 intents a fresh demo run creates (Gemini may pick different names on a
# later run — pass --names to target whatever was actually created).
DEMO_INTENT_NAMES = [
    "ResetAdamPrivilegedPassword",
    "TroubleshootGlobalProtectDisconnects",
    "SoftwareAccessAccessAfter",
    "RequestSmartsheetLicense",
]


def _load_dotenv() -> None:
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args():
    parser = argparse.ArgumentParser(description="Delete Lex V2 intents by name.")
    parser.add_argument("--names", nargs="+", default=DEMO_INTENT_NAMES,
                        help="Intent names to delete (default: the 4 demo intents).")
    parser.add_argument("--bot-id", help="Lex bot ID (default: BOT_ID from .env).")
    parser.add_argument("--region", help="AWS region (default: AWS_REGION from .env).")
    parser.add_argument("--locale-id", help="Lex locale (default: LOCALE_ID from .env or en_US).")
    parser.add_argument("--bot-version", default="DRAFT", help="Lex bot version (default: DRAFT).")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete. Without this, only prints what would be deleted.")
    return parser.parse_args()


def main() -> None:
    _load_dotenv()
    args = parse_args()

    bot_id = args.bot_id or os.environ.get("BOT_ID", "")
    region = args.region or os.environ.get("AWS_REGION", "ap-southeast-2")
    locale_id = args.locale_id or os.environ.get("LOCALE_ID", "en_US")
    if not bot_id:
        print("No bot id. Pass --bot-id or set BOT_ID in .env.")
        sys.exit(1)

    boto3, ClientError = loader.require_boto3()
    client = boto3.client("lexv2-models", region_name=region)

    existing = loader.existing_intents_by_name(client, bot_id, args.bot_version, locale_id)
    targets = [(name, existing.get(name)) for name in args.names]

    found = [(n, i) for n, i in targets if i]
    missing = [n for n, i in targets if not i]

    print(f"Bot {bot_id} / {locale_id} / {args.bot_version} in {region}")
    print(f"  {len(found)} of {len(args.names)} requested intents found:")
    for name, intent_id in found:
        print(f"    - {name}  ({intent_id})")
    if missing:
        print(f"  not found (already gone?): {', '.join(missing)}")

    if not found:
        print("\nNothing to delete.")
        return

    if not args.apply:
        print("\nDry run only. Add --apply to actually delete these intents.")
        return

    print()
    deleted = 0
    for name, intent_id in found:
        try:
            client.delete_intent(intentId=intent_id, botId=bot_id,
                                 botVersion=args.bot_version, localeId=locale_id)
            print(f"  deleted {name}")
            deleted += 1
        except ClientError as error:
            detail = error.response.get("Error", {})
            print(f"  FAILED {name}: {detail.get('Code')} - {detail.get('Message')}")

    print(f"\nDeleted {deleted}/{len(found)} intents. "
          "Rebuild the locale (Lex console) if you want the change reflected in a build.")


if __name__ == "__main__":
    main()
