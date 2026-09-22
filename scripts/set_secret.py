r"""
Store the SQL Server password in the Windows Credential Manager instead of
leaving it in plaintext in .env.

The value is encrypted by Windows under your user account (DPAPI) and is read
back automatically by src/settings.py when MSSQL_PASSWORD is empty.

Run:   .venv\Scripts\python scripts\set_secret.py
Then:  remove MSSQL_PASSWORD from .env (leave the key blank).
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import keyring

from src.settings import KEYRING_SERVICE


def main() -> int:
    print(f"Credential store : {keyring.get_keyring().__class__.__name__}")
    print(f"Service name     : {KEYRING_SERVICE}\n")

    username = input("SQL Server login name: ").strip()
    if not username:
        print("Aborted: no username given.")
        return 1

    # getpass keeps the password off the screen and out of the shell history.
    password = getpass.getpass("Password (not echoed): ")
    if not password:
        print("Aborted: empty password.")
        return 1

    keyring.set_password(KEYRING_SERVICE, username, password)
    stored = keyring.get_password(KEYRING_SERVICE, username)
    if stored == password:
        print(f"\nStored. Now set in .env:\n"
              f"  MSSQL_AUTH_MODE=sql\n"
              f"  MSSQL_USERNAME={username}\n"
              f"  MSSQL_PASSWORD=          <- leave blank\n")
        return 0

    print("\nVerification failed - the password was not stored correctly.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
