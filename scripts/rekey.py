"""
Re-encrypt a Bachat DB under a new secret (e.g. moving off the legacy built-in key).

    OLD_KEY=... NEW_KEY=... python scripts/rekey.py transactions.db rekeyed.db

The source file is never modified; the result is written to a new file.
Omit OLD_KEY to read a DB created with the legacy default key.
"""
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transaction_ml.storage.db import EncryptedStorage  # noqa: E402
from transaction_ml.system import LEGACY_DEFAULT_SECRET, derive_fernet_key  # noqa: E402


def rekey(src: str, dst: str, old_secret: str, new_secret: str) -> int:
    if os.path.exists(dst):
        raise SystemExit(f"{dst} already exists; refusing to overwrite")
    shutil.copy2(src, dst)
    old = EncryptedStorage(dst, derive_fernet_key(old_secret))   # also migrates the schema
    rows, unparsed = old.get_all_transactions(), old.get_unparsed_sms()
    new = EncryptedStorage(dst, derive_fernet_key(new_secret))
    new.insert_transactions(rows)
    with new._connect() as conn:
        conn.execute("DELETE FROM unparsed_sms")
    for received_at, text in unparsed:
        new.save_unparsed_sms(text, datetime.fromisoformat(received_at) if received_at else None)
    for f in os.listdir(os.path.dirname(os.path.abspath(dst))):  # backups taken during migration hold old-key data
        if f.startswith(os.path.basename(dst) + ".bak-"):
            os.remove(os.path.join(os.path.dirname(os.path.abspath(dst)), f))
    return len(rows)


if __name__ == "__main__":
    if len(sys.argv) != 3 or not os.environ.get("NEW_KEY"):
        raise SystemExit(__doc__)
    n = rekey(sys.argv[1], sys.argv[2], os.environ.get("OLD_KEY", LEGACY_DEFAULT_SECRET), os.environ["NEW_KEY"])
    print(f"Re-encrypted {n} transactions into {sys.argv[2]}")
