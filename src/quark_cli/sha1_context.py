"""Native SHA-1 chaining words for the existing OSS continuation protocol."""
import ctypes
import ctypes.util
import functools
import sys
from pathlib import Path


class _Context(ctypes.Structure):
    # Public OpenSSL SHA_CTX layout (include/openssl/sha.h).
    _fields_ = [(f"h{i}", ctypes.c_uint) for i in range(5)] + [
        ("Nl", ctypes.c_uint), ("Nh", ctypes.c_uint),
        ("data", ctypes.c_uint * 16), ("num", ctypes.c_uint)]


@functools.lru_cache(maxsize=1)
def native_update():
    if sys.platform == "win32":
        import _ssl
        roots = [Path(_ssl.__file__).parent, Path(sys.base_prefix) / "DLLs", Path(sys.base_prefix) / "Library" / "bin"]
        if getattr(sys, "frozen", False):
            roots = [Path(sys._MEIPASS)]
        candidates = [root / name for root in roots for name in
            ("libcrypto-3-x64.dll", "libcrypto-3.dll", "libcrypto-1_1-x64.dll", "libcrypto-1_1.dll")]
        candidates = [str(path) for path in candidates if path.is_file()]
    else:
        name = ctypes.util.find_library("crypto")
        candidates = [name] if name else []
    for name in candidates:
        try:
            library = ctypes.CDLL(name)
            update = library.SHA1_Update
            update.argtypes = [ctypes.POINTER(_Context), ctypes.c_void_p, ctypes.c_size_t]
            update.restype = ctypes.c_int
            return update
        except (OSError, AttributeError):
            continue
    return None


def chaining_words(words, length, data):
    update = native_update()
    if update is None:
        return None
    context = _Context()
    for index, word in enumerate(words):
        setattr(context, f"h{index}", word)
    bits = length * 8
    context.Nl, context.Nh = bits & 0xffffffff, (bits >> 32) & 0xffffffff
    if update(ctypes.byref(context), data, len(data)) != 1 or context.num:
        raise RuntimeError("OpenSSL SHA-1 continuation failed")
    return [getattr(context, f"h{index}") for index in range(5)]
