import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook


LEX_NAME_PATTERN = re.compile(r"^([0-9a-zA-Z][_-]?){1,100}$")
SLOT_PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")
NO_RESPONSE_VALUES = {
    "",
    "(no response configured)",
    "no response configured",
    "none",
    "n/a",
}
MAX_RESPONSE_MESSAGE_GROUPS = 5
DEFAULT_EXCEL = "Intents_20_List.xlsx"
DEFAULT_SHEET = "Initial Intents"


def normalize_text(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_utterance(value):
    return re.sub(r"\s+", " ", normalize_text(value))


def normalize_response(value):
    text = normalize_text(value).replace("\r\n", "\n").replace("\r", "\n")

    if text.lower() in NO_RESPONSE_VALUES:
        return ""

    lines = [line.strip() for line in text.splitlines()]
    normalized_lines = []
    previous_blank = False

    for line in lines:
        if not line:
            if not previous_blank:
                normalized_lines.append("")
            previous_blank = True
            continue

        line = re.sub(r"(?<=[a-z0-9])\.(?=[A-Z])", ". ", line)
        normalized_lines.append(line)
        previous_blank = False

    text = "\n".join(normalized_lines).strip()

    title_match = re.match(r"^\[([^\]]+)\](?:\n+|$)", text)
    if title_match:
        title = title_match.group(1).strip()
        remainder = text[title_match.end():].strip()
        text = f"{title}\n\n{remainder}" if remainder else title

    return text


def response_message_groups(text):
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]

    if not paragraphs:
        return []

    if len(paragraphs) > MAX_RESPONSE_MESSAGE_GROUPS:
        head = paragraphs[:MAX_RESPONSE_MESSAGE_GROUPS - 1]
        tail = "\n\n".join(paragraphs[MAX_RESPONSE_MESSAGE_GROUPS - 1:])
        paragraphs = [*head, tail]

    return [
        {
            "message": {
                "plainTextMessage": {
                    "value": paragraph,
                }
            }
        }
        for paragraph in paragraphs
    ]


def pascal_token(token):
    if token.isupper():
        return token

    return token[:1].upper() + token[1:]


def to_lex_intent_name(name):
    raw_name = normalize_text(name)

    if LEX_NAME_PATTERN.fullmatch(raw_name):
        return raw_name

    parts = re.findall(r"[0-9A-Za-z]+", raw_name)
    sanitized = "".join(pascal_token(part) for part in parts)

    if not sanitized:
        sanitized = "ImportedIntent"

    if not sanitized[0].isalnum():
        sanitized = f"Intent{sanitized}"

    return sanitized[:100]


def make_unique_name(name, used_names):
    if name not in used_names:
        used_names.add(name)
        return name

    base = name[:95]
    counter = 2

    while True:
        candidate = f"{base}{counter}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1


def read_intents(excel_path, sheet_name):
    workbook = load_workbook(excel_path, data_only=True, read_only=True)

    if sheet_name not in workbook.sheetnames:
        available = ", ".join(workbook.sheetnames)
        raise ValueError(f"Sheet '{sheet_name}' not found. Available sheets: {available}")

    worksheet = workbook[sheet_name]
    intents = []
    current_intent = None
    used_names = set()

    for row in worksheet.iter_rows(min_row=3, values_only=True):
        original_name = normalize_text(row[1] if len(row) > 1 else None)
        category = normalize_text(row[2] if len(row) > 2 else None)
        utterance = normalize_utterance(row[3] if len(row) > 3 else None)
        uses_lambda = normalize_text(row[4] if len(row) > 4 else None).lower() == "yes"
        response = normalize_response(row[5] if len(row) > 5 else None)

        if original_name:
            lex_name = make_unique_name(to_lex_intent_name(original_name), used_names)
            current_intent = {
                "original_name": original_name,
                "intent_name": lex_name,
                "category": category,
                "uses_lambda": uses_lambda,
                "response": response,
                "utterances": [],
                "skipped_slot_utterances": [],
            }
            intents.append(current_intent)

        if current_intent and utterance:
            if SLOT_PLACEHOLDER_PATTERN.search(utterance):
                current_intent["skipped_slot_utterances"].append(utterance)
            else:
                current_intent["utterances"].append(utterance)

    for intent in intents:
        seen = set()
        unique_utterances = []

        for utterance in intent["utterances"]:
            if utterance not in seen:
                seen.add(utterance)
                unique_utterances.append(utterance)

        intent["utterances"] = unique_utterances

    return intents


def response_spec(text):
    return {
        "messageGroups": response_message_groups(text),
        "allowInterrupt": True,
    }


def intent_payload(intent, lambda_mode):
    description_parts = [
        "Imported from Intents_20_List.xlsx.",
        f"Original name: {intent['original_name']}.",
    ]

    if intent["category"]:
        description_parts.append(f"Category: {intent['category']}.")

    description_parts.append(f"Uses Lambda in source sheet: {'Yes' if intent['uses_lambda'] else 'No'}.")

    payload = {
        "intentName": intent["intent_name"],
        "description": " ".join(description_parts)[:2000],
        "sampleUtterances": [
            {
                "utterance": utterance,
            }
            for utterance in intent["utterances"]
        ],
    }

    if intent["original_name"] != intent["intent_name"]:
        payload["intentDisplayName"] = intent["original_name"][:100]

    if intent["response"]:
        payload["intentClosingSetting"] = {
            "active": True,
            "closingResponse": response_spec(intent["response"]),
        }

    if lambda_mode == "marked" and intent["uses_lambda"]:
        payload["fulfillmentCodeHook"] = {
            "enabled": True,
            "active": True,
        }

    return payload


def find_duplicate_utterances(intents):
    utterance_to_intents = {}

    for intent in intents:
        for utterance in intent["utterances"]:
            utterance_to_intents.setdefault(utterance.lower(), set()).add(intent["intent_name"])

    return {
        utterance: sorted(intent_names)
        for utterance, intent_names in utterance_to_intents.items()
        if len(intent_names) > 1
    }


def print_summary(intents, duplicate_utterances):
    renamed = [
        intent
        for intent in intents
        if intent["original_name"] != intent["intent_name"]
    ]
    utterance_count = sum(len(intent["utterances"]) for intent in intents)
    lambda_marked_count = sum(1 for intent in intents if intent["uses_lambda"])
    skipped_slot_utterance_count = sum(
        len(intent["skipped_slot_utterances"])
        for intent in intents
    )
    names = [intent["intent_name"] for intent in intents]
    duplicated_names = [name for name, count in Counter(names).items() if count > 1]

    print(f"Loaded {len(intents)} intents from Excel.")
    print(f"Loaded {utterance_count} sample utterances.")
    print(f"Skipped {skipped_slot_utterance_count} slot-placeholder utterances.")
    print(f"Spreadsheet marks {lambda_marked_count} intents as Uses Lambda = Yes.")

    if renamed:
        print("\nLex-safe name mapping:")
        for intent in renamed:
            print(f"  {intent['original_name']} -> {intent['intent_name']}")

    if duplicated_names:
        print("\nERROR: duplicate Lex intent names after sanitizing:")
        for name in duplicated_names:
            print(f"  {name}")

    if duplicate_utterances:
        print("\nWARNING: duplicate utterances across intents may make Lex matching ambiguous:")
        for utterance, intent_names in duplicate_utterances.items():
            print(f"  {utterance!r}: {', '.join(intent_names)}")

    if skipped_slot_utterance_count:
        print("\nSkipped slot-placeholder utterances because FAQ import does not create Lex slots:")
        for intent in intents:
            for utterance in intent["skipped_slot_utterances"]:
                print(f"  {intent['intent_name']}: {utterance}")


def require_boto3():
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required for --apply. Install dependencies with: "
            "python -m pip install boto3 openpyxl"
        ) from exc

    return boto3, ClientError


def existing_intents_by_name(client, bot_id, bot_version, locale_id):
    existing = {}
    request = {
        "botId": bot_id,
        "botVersion": bot_version,
        "localeId": locale_id,
    }

    while True:
        page = client.list_intents(**request)

        for summary in page.get("intentSummaries", []):
            name = summary.get("intentName")
            intent_id = summary.get("intentId")
            if name and intent_id:
                existing[name] = intent_id

        next_token = page.get("nextToken")
        if not next_token:
            break

        request["nextToken"] = next_token

    return existing


def apply_intents(args, intents):
    boto3, ClientError = require_boto3()
    client = boto3.client("lexv2-models", region_name=args.region)
    existing = existing_intents_by_name(client, args.bot_id, args.bot_version, args.locale_id)
    created = 0
    updated = 0

    for intent in intents:
        payload = intent_payload(intent, args.lambda_mode)
        base_request = {
            "botId": args.bot_id,
            "botVersion": args.bot_version,
            "localeId": args.locale_id,
        }

        try:
            if intent["intent_name"] in existing:
                client.update_intent(
                    **base_request,
                    intentId=existing[intent["intent_name"]],
                    **payload,
                )
                updated += 1
                print(f"updated {intent['intent_name']}")
            else:
                client.create_intent(
                    **base_request,
                    **payload,
                )
                created += 1
                print(f"created {intent['intent_name']}")
        except ClientError as exc:
            error = exc.response.get("Error", {})
            raise RuntimeError(
                f"Failed to load intent {intent['intent_name']}: "
                f"{error.get('Code', 'ClientError')} - {error.get('Message', str(exc))}"
            ) from exc

    print(f"\nLex load complete: {created} created, {updated} updated.")

    if args.build:
        build_bot_locale(client, args.bot_id, args.bot_version, args.locale_id, args.wait_build)


def build_bot_locale(client, bot_id, bot_version, locale_id, wait_build):
    print("\nStarting Lex locale build...")
    client.build_bot_locale(
        botId=bot_id,
        botVersion=bot_version,
        localeId=locale_id,
    )

    if not wait_build:
        print("Build started. Check Lex console for build status.")
        return

    terminal_statuses = {"Built", "ReadyExpressTesting", "Failed"}
    while True:
        response = client.describe_bot_locale(
            botId=bot_id,
            botVersion=bot_version,
            localeId=locale_id,
        )
        status = response.get("botLocaleStatus")
        print(f"Build status: {status}")

        if status in terminal_statuses:
            if status == "Failed":
                reasons = response.get("failureReasons") or []
                raise RuntimeError("Lex locale build failed: " + "; ".join(reasons))

            print("Lex locale build completed.")
            return

        time.sleep(15)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load Project IVY Excel intents into an Amazon Lex V2 bot locale."
    )
    parser.add_argument("--excel", default=DEFAULT_EXCEL, help="Path to Intents_20_List.xlsx.")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help="Worksheet name to read.")
    parser.add_argument("--region", default="ap-southeast-2", help="AWS region for Lex.")
    parser.add_argument("--bot-id", help="Lex V2 bot ID.")
    parser.add_argument("--bot-version", default="DRAFT", help="Lex bot version. Keep DRAFT for editing.")
    parser.add_argument("--locale-id", default="en_US", help="Lex locale ID.")
    parser.add_argument(
        "--lambda-mode",
        choices=["none", "marked"],
        default="none",
        help="Use 'marked' only if your Lex alias already has a fulfillment Lambda configured.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually create/update intents in AWS.")
    parser.add_argument("--build", action="store_true", help="Build the Lex locale after loading intents.")
    parser.add_argument("--wait-build", action="store_true", help="Wait until the Lex locale build finishes.")
    parser.add_argument("--json-preview", help="Write the parsed Lex payload preview to this JSON file.")

    return parser.parse_args()


def main():
    args = parse_args()
    excel_path = Path(args.excel)

    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    intents = read_intents(excel_path, args.sheet)
    duplicate_utterances = find_duplicate_utterances(intents)
    print_summary(intents, duplicate_utterances)

    if args.json_preview:
        preview = [intent_payload(intent, args.lambda_mode) for intent in intents]
        Path(args.json_preview).write_text(json.dumps(preview, indent=2), encoding="utf-8")
        print(f"\nWrote JSON preview: {args.json_preview}")

    if not args.apply:
        print("\nDry run only. Add --apply to create/update these intents in Lex.")
        return

    if not args.bot_id:
        raise ValueError("--bot-id is required with --apply")

    apply_intents(args, intents)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
