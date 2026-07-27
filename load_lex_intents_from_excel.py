import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path


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
NO_UTTERANCE_VALUES = {
    "",
    "(no utterances)",
    "no utterances",
    "n/a",
    "none",
}
# Lex V2 reserves these intent names (the bot auto-creates FallbackIntent; the
# AMAZON.* names are built-ins), so we skip them instead of failing the load.
LEX_RESERVED_INTENT_NAMES = {"fallbackintent"}
# Anchor the default workbook to this script's directory so the loader works no
# matter what the current working directory is (previously a bare filename that
# only resolved when run from the repo root).
DEFAULT_EXCEL = str(Path(__file__).resolve().parent / "Intents_20_List.xlsx")
DEFAULT_SHEET = "Initial Intents"

# Columns are located by header name, not fixed position, so the reader works
# regardless of extra columns. The full export inserts a "Total Utts" column
# that shifts "Uses Lambda" and "Response / Answer"; matching on the header name
# keeps both this and the legacy 20-intent sheet correct. Falls back to the
# legacy positional layout when no recognizable header row is found.
COLUMN_ALIASES = {
    "intent_name": ("intent name", "name"),
    "category": ("category",),
    "utterance": ("utterance", "utterances", "sample utterance"),
    "uses_lambda": ("uses lambda", "lambda"),
    "response": ("response / answer", "response/answer", "response", "answer"),
}
LEGACY_COLUMNS = {"intent_name": 1, "category": 2, "utterance": 3, "uses_lambda": 4, "response": 5}


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

    # Lex reserves "{" / "}" for slot placeholders and rejects them in prompts or
    # responses, so neutralize any stray braces from the source content.
    text = text.replace("{", "(").replace("}", ")")

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


def resolve_column_map(rows, max_scan=5):
    """Locate the header row and map field -> column index by header name.

    Returns (column_map, data_start_row_1indexed). Falls back to the legacy
    positional layout (data starting at row 3) when no recognizable header row
    is found in the first `max_scan` rows.
    """
    for row_index, row in enumerate(rows[:max_scan], start=1):
        headers = {
            normalize_text(cell).lower(): index
            for index, cell in enumerate(row)
            if cell is not None
        }
        if any(alias in headers for alias in COLUMN_ALIASES["intent_name"]):
            column_map = {}
            for field, aliases in COLUMN_ALIASES.items():
                for alias in aliases:
                    if alias in headers:
                        column_map[field] = headers[alias]
                        break
            return column_map, row_index + 1

    return dict(LEGACY_COLUMNS), 3


def _rows_from_xlsx(path, sheet_name):
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True, read_only=True)

    if sheet_name not in workbook.sheetnames:
        available = ", ".join(workbook.sheetnames)
        raise ValueError(f"Sheet '{sheet_name}' not found. Available sheets: {available}")

    worksheet = workbook[sheet_name]
    return list(worksheet.iter_rows(values_only=True))


def _rows_from_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return [tuple(row) for row in csv.reader(handle)]


def read_intents(path, sheet_name=None):
    """Read intents from either a .csv or .xlsx export (same row/column shape).

    `sheet_name` is only used for .xlsx files; ignored for .csv.
    """
    path = Path(path)
    if path.suffix.lower() == ".csv":
        rows = _rows_from_csv(path)
    else:
        rows = _rows_from_xlsx(path, sheet_name or DEFAULT_SHEET)

    columns, data_start = resolve_column_map(rows)

    def cell(row, field):
        index = columns.get(field)
        if index is None or index >= len(row):
            return None
        return row[index]

    intents = []
    current_intent = None
    used_names = set()

    for row in rows[data_start - 1:]:
        original_name = normalize_text(cell(row, "intent_name"))
        category = normalize_text(cell(row, "category"))
        utterance = normalize_utterance(cell(row, "utterance"))
        uses_lambda = normalize_text(cell(row, "uses_lambda")).lower() == "yes"
        response = normalize_response(cell(row, "response"))

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

        if current_intent and utterance and utterance.lower() not in NO_UTTERANCE_VALUES:
            if "{" in utterance or "}" in utterance:
                # Lex can't take a plain utterance containing braces (slot syntax).
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
        "Imported from Project IVY intent export.",
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


def dedupe_cross_intent_utterances(intents):
    """Drop utterances that appear in more than one intent so each is unique
    across the locale (a Lex V2 build requirement).

    A shared utterance is kept by the *more specific* intent -- the one with the
    fewest utterances -- so a broad catch-all (e.g. ApplicationLicenses) doesn't
    strip specific intents (e.g. SmartsheetLicense) of their identity. Intents
    left with no utterances afterward are skipped/pruned at apply time.
    """
    seen = set()
    removed = 0
    for intent in sorted(intents, key=lambda item: len(item["utterances"])):
        kept = []
        for utterance in intent["utterances"]:
            key = utterance.lower()
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            kept.append(utterance)
        intent["utterances"] = kept
    return removed


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

    print(f"Loaded {len(intents)} intents from the export.")
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


def is_reserved_lex_name(name):
    """Lex V2 rejects creating intents whose name is reserved or built-in."""
    lowered = normalize_text(name).lower()
    return lowered in LEX_RESERVED_INTENT_NAMES or lowered.startswith("amazon.")


def apply_intents(args, intents):
    boto3, ClientError = require_boto3()
    client = boto3.client("lexv2-models", region_name=args.region)
    existing = existing_intents_by_name(client, args.bot_id, args.bot_version, args.locale_id)
    created = 0
    updated = 0
    skipped_reserved = 0
    skipped_empty = 0
    pruned = 0
    failures = []

    base_request = {
        "botId": args.bot_id,
        "botVersion": args.bot_version,
        "localeId": args.locale_id,
    }

    for intent in intents:
        name = intent["intent_name"]

        if is_reserved_lex_name(name):
            skipped_reserved += 1
            print(f"skipped {name} (reserved Lex name; the bot already has this built-in)")
            continue

        # No sample utterances -> Lex can't NLU-match it and the locale won't
        # build. Prune it (if it already exists and --prune-empty is set) or skip.
        if not intent["utterances"]:
            if args.prune_empty and name in existing:
                try:
                    client.delete_intent(intentId=existing[name], **base_request)
                    pruned += 1
                    print(f"pruned {name} (no utterances in the export)")
                except ClientError as exc:
                    error = exc.response.get("Error", {})
                    failures.append((name, f"prune: {error.get('Code')} - {error.get('Message')}"))
                    print(f"FAILED to prune {name}")
            else:
                skipped_empty += 1
                print(f"skipped {name} (no sample utterances)")
            continue

        payload = intent_payload(intent, args.lambda_mode)

        try:
            if name in existing:
                client.update_intent(**base_request, intentId=existing[name], **payload)
                updated += 1
                print(f"updated {name}")
            else:
                client.create_intent(**base_request, **payload)
                created += 1
                print(f"created {name}")
        except ClientError as exc:
            error = exc.response.get("Error", {})
            message = f"{error.get('Code', 'ClientError')} - {error.get('Message', str(exc))}"
            failures.append((name, message))
            print(f"FAILED {name}: {message}")

    print(
        f"\nLex load: {created} created, {updated} updated, "
        f"{skipped_reserved} reserved-skip, {skipped_empty} empty-skip, {pruned} pruned."
    )

    if failures:
        print(f"\n{len(failures)} intent(s) FAILED (everything else loaded):")
        for failed_name, message in failures:
            print(f"  {failed_name}: {message}")

    build_status = None
    if args.build:
        build_status = build_bot_locale(
            client, args.bot_id, args.bot_version, args.locale_id, args.wait_build
        )

    return {
        "created": created,
        "updated": updated,
        "skipped_reserved": skipped_reserved,
        "skipped_empty": skipped_empty,
        "pruned": pruned,
        "failures": [{"intent": name, "error": message} for name, message in failures],
        "build": build_status,
    }


def build_bot_locale(client, bot_id, bot_version, locale_id, wait_build):
    print("\nStarting Lex locale build...")
    client.build_bot_locale(
        botId=bot_id,
        botVersion=bot_version,
        localeId=locale_id,
    )

    if not wait_build:
        print("Build started. Check Lex console for build status.")
        return "Building"

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
            return status

        time.sleep(15)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load Project IVY Excel intents into an Amazon Lex V2 bot locale."
    )
    parser.add_argument(
        "--file", dest="excel", default=DEFAULT_EXCEL,
        help="Path to the intent export to load (.xlsx or .csv).",
    )
    parser.add_argument("--excel", dest="excel", help=argparse.SUPPRESS)  # back-compat alias
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help="Worksheet name to read (.xlsx only).")
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
    parser.add_argument(
        "--prune-empty",
        action="store_true",
        help="Delete existing intents that have no sample utterances in the export "
             "(clears the placeholder/control intents so the locale can build).",
    )
    parser.add_argument(
        "--dedupe-utterances",
        action="store_true",
        help="Drop utterances shared across intents (keep the first occurrence). "
             "Lex requires utterances to be unique per locale; use this if the build reports duplicates.",
    )
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

    if args.dedupe_utterances:
        removed = dedupe_cross_intent_utterances(intents)
        print(f"Deduplicated {removed} utterances shared across intents (kept the first occurrence).\n")

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
