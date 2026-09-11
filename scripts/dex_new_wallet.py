#!/usr/bin/env python3
"""Create a trading wallet whose key never leaves this machine.

The key is generated here, written straight into ``.env`` at 0600, and never
printed. Only the address is shown -- that is the part that is safe to share,
paste into a block explorer, or send to anyone.

A private key that has been pasted into a chat, an issue, a terminal someone
screen-shared, or any file that syncs to a cloud is spent: the only fix is a new
wallet, because the old one can be emptied by anyone who ever saw it.

    scripts/dex_new_wallet.py            # create and store
    scripts/dex_new_wallet.py --import   # store a key you already have
    scripts/dex_new_wallet.py --force    # replace the key already in .env

``--import`` prompts for the key instead of taking it as an argument, so it
never reaches the shell history, the process list, or the screen.
"""

import argparse
import getpass
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_account import Account  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
KEY_LINE = re.compile(r"^RH_PRIVATE_KEY\s*=\s*(.*)$", re.MULTILINE)


def store(private_key: str, *, force: bool) -> None:
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    existing = KEY_LINE.search(text)
    if existing and existing.group(1).strip() and not force:
        raise SystemExit(
            "RH_PRIVATE_KEY is already set in .env. Pass --force to replace it "
            "-- and move any funds off the old wallet first, because replacing "
            "the key here does not empty it."
        )

    line = f"RH_PRIVATE_KEY={private_key}"
    text = KEY_LINE.sub(line, text) if existing else (
        text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    )
    # Write restricted from the start: never leave a key world-readable, not
    # even for the moment between writing and chmod.
    handle = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as file:
        file.write(text)
    os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)


def prompt_for_key() -> Account:
    """Read an existing key without echoing it or putting it in an argument."""
    entered = getpass.getpass("Private key (input hidden): ").strip()
    if not entered:
        raise SystemExit("Nothing entered.")
    try:
        account = Account.from_key(entered)
    except Exception:
        raise SystemExit(
            "That is not a valid private key: expected 64 hex characters, "
            "with or without a leading 0x."
        ) from None
    return account


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--import", dest="import_key", action="store_true",
        help="store a key you already have, entered at a hidden prompt",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="replace an RH_PRIVATE_KEY that is already in .env",
    )
    args = parser.parse_args()

    account = prompt_for_key() if args.import_key else Account.create()
    store(account.key.hex(), force=args.force)
    # account.key goes out of scope here and is never printed or logged.

    print()
    print("Trading wallet imported." if args.import_key else "New trading wallet created.")
    print()
    print(f"  Address: {account.address}")
    print(f"  Key:     stored in {ENV_PATH} (0600), not shown")
    print()
    if args.import_key:
        print("Check that address is the one you meant before funding it.")
    else:
        print("Fund this address on Robinhood Chain with only what it may lose.")
    print("The key is on this machine so a worker can sign unattended -- that is")
    print("exactly why it must never be your main wallet's key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
