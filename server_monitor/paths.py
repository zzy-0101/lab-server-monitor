"""Machine-local defaults; no developer-specific absolute paths."""
from pathlib import Path


def default_identity_file(home=None):
    root = Path(home) if home is not None else Path.home()
    for name in ('id_ed25519_cnot', 'id_ed25519', 'id_rsa'):
        path = root / '.ssh' / name
        if path.is_file():
            return str(path)
    return ''
