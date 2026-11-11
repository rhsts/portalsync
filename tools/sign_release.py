"""Sign a PortalSync release zip, so the updater can tell the release is yours (portalsync.py: UPDATE_KEY, signed_by).
Copyright (C) 2026 rhsts. SPDX-License-Identifier: GPL-2.0-or-later (see LICENSE).

  python tools/sign_release.py keygen <key file>                  once: makes the signing key, prints UPDATE_KEY
  python tools/sign_release.py <key file> PortalSync-v1.8.0.zip   each release: writes PortalSync-v1.8.0.zip.sig

Put the printed UPDATE_KEY line in portalsync.py, and upload each .sig next to its zip on the release page. Keep the
key file out of the repository and keep a copy of it: PortalSync versions that carry its UPDATE_KEY take no update
signed with another key. ECDSA P-256 over the zip's SHA-256, done by Windows (bcrypt.dll): no extra package.
"""
import ctypes
import hashlib
import sys

bc = ctypes.WinDLL('bcrypt')


def keygen():
    """A new key pair: (the private key blob for the key file, the public X and Y for UPDATE_KEY)."""
    alg, h, n = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_ulong()
    blob = ctypes.create_string_buffer(8 + 96)  # BCRYPT_ECCKEY_BLOB: magic, key size, then X, Y and the secret d
    if (bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'ECDSA_P256', None, 0)
            or bc.BCryptGenerateKeyPair(alg, ctypes.byref(h), 256, 0) or bc.BCryptFinalizeKeyPair(h, 0)
            or bc.BCryptExportKey(h, None, 'ECCPRIVATEBLOB', blob, len(blob), ctypes.byref(n), 0)):
        raise OSError('Windows could not make an ECDSA P-256 key')
    return blob.raw, blob.raw[8:72]


def sign(private, digest):
    """The 64-byte signature (r, s) of digest (a SHA-256) with the private key blob."""
    alg, h, n = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_ulong()
    signature = ctypes.create_string_buffer(64)
    if (bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'ECDSA_P256', None, 0)
            or bc.BCryptImportKeyPair(alg, None, 'ECCPRIVATEBLOB', ctypes.byref(h), private, len(private), 0)
            or bc.BCryptSignHash(h, None, digest, len(digest), signature, len(signature), ctypes.byref(n), 0)):
        raise OSError('Windows could not sign with this key file')
    return signature.raw


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'keygen':
        private, public = keygen()
        with open(sys.argv[2], 'x') as f:  # 'x': never over an existing key
            f.write(private.hex())
        print(f"key saved to {sys.argv[2]}; put this line in portalsync.py:\nUPDATE_KEY = '{public.hex()}'")
    elif len(sys.argv) == 3:
        with open(sys.argv[1]) as f:
            private = bytes.fromhex(f.read().strip())
        with open(sys.argv[2], 'rb') as f:
            digest = hashlib.sha256(f.read()).digest()
        with open(sys.argv[2] + '.sig', 'wb') as f:
            f.write(sign(private, digest))
        print(f'{sys.argv[2]}.sig written: upload it next to the zip')
    else:
        raise SystemExit(__doc__)
