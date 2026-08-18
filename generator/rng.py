"""The source of randomness.

Every bit of determinism in this project rests on this file, which is why it is
small and separate.

Two rules; everything else follows from them.

**No global `random`.** Every call gets its own `random.Random` instance, seeded
from a meaningful set of keys. Global state would mean the result depends on the
order of calls, and the order of calls changes with the first edit anyone makes.

**The seed is derived from a string with blake2b, not with `hash()`.** The
built-in `hash()` for strings is randomised from run to run (PYTHONHASHSEED),
and reproducibility built on it would have broken silently — the nastiest kind
of breakage.

Hence the property it was all built for: streams are named. Orders for a day
take the "orders" stream, traffic takes "traffic", and adding a new random call
to one stream does not shift the other. Without that, any edit to
the generator would change all the data at once, and "before / after" could not
be compared.
"""

from __future__ import annotations

import hashlib
import random


def derive_seed(*parts: object) -> int:
    """A stable 64-bit seed from arbitrary keys."""
    material = ":".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(material, digest_size=8).digest()
    return int.from_bytes(digest, "big")


def stream(*parts: object) -> random.Random:
    """A separate generator for a named stream."""
    return random.Random(derive_seed(*parts))
