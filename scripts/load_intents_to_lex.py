from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


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


def main() -> None:
    _load_dotenv()

    # Reuse the existing loader's argument parsing and pipeline untouched;
    # just fill in bot-id/region/locale defaults from .env when not passed.
    import load_lex_intents_from_excel as loader

    sys.argv = [sys.argv[0]] + _apply_env_defaults(sys.argv[1:])
    loader.main()


def _apply_env_defaults(argv):
    """Inject --bot-id/--region/--locale-id from .env if the caller omitted them."""
    has = lambda flag: any(a == flag or a.startswith(flag + "=") for a in argv)
    extra = []
    if not has("--bot-id") and os.environ.get("BOT_ID"):
        extra += ["--bot-id", os.environ["BOT_ID"]]
    if not has("--region") and os.environ.get("AWS_REGION"):
        extra += ["--region", os.environ["AWS_REGION"]]
    if not has("--locale-id") and os.environ.get("LOCALE_ID"):
        extra += ["--locale-id", os.environ["LOCALE_ID"]]
    return argv + extra


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
