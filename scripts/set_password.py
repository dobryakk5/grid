#!/usr/bin/env python3
"""Print the AUTH_* lines for .env. Never writes the file or echoes the password.

    .venv/bin/python scripts/set_password.py
"""

import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import hash_password  # noqa: E402


def main() -> int:
    password = getpass.getpass("Пароль оператора: ")
    if password != getpass.getpass("Ещё раз: "):
        print("Пароли не совпали.", file=sys.stderr)
        return 1
    if len(password) < 12:
        # A short password behind a public port is the whole attack.
        print("Слишком короткий пароль: минимум 12 символов.", file=sys.stderr)
        return 1
    print("\nДобавьте в .env (и перезапустите API):\n")
    print(f"AUTH_SECRET={secrets.token_urlsafe(48)}")
    print(f"AUTH_PASSWORD_HASH={hash_password(password)}")
    print(f"AUTH_SERVICE_TOKEN={secrets.token_urlsafe(32)}")
    print("\nAUTH_SERVICE_TOKEN — для сборщика FOMO; передайте его как GRID_API_TOKEN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
