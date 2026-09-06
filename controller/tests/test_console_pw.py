"""
The console password record, and the vectors emOS's init has to agree with.

The C implementation in emos/init/init.c reimplements this by hand — it is a
static binary with no crypto library — so the two can only be known to agree by
hashing the same inputs and comparing. KNOWN_VECTORS is that contract: if a
change here moves them, the device stops accepting a password it was told to
accept, and nothing else in either tree would notice.
"""

import hashlib

import pytest

import em_console_pw as pw


# ── The cross-language contract ───────────────────────────────────────────────
#
# Deliberately at a low iteration count so the C side can be checked by hand
# without waiting, and so a mistake in the LOOP shows up rather than being
# buried under a hundred thousand rounds. The iteration count lives in the
# record, so these are as real as any other record.
KNOWN_VECTORS = [
    # (password, salt hex, iterations, expected hash hex)
    ("hunter2", "0001020304050607", 1,
     hashlib.sha256(bytes.fromhex("0001020304050607") + b"hunter2").hexdigest()),
    ("hunter2", "0001020304050607", 2,
     hashlib.sha256(
         hashlib.sha256(bytes.fromhex("0001020304050607") + b"hunter2").digest()
     ).hexdigest()),
]


@pytest.mark.parametrize("password,salt_hex,iterations,expected", KNOWN_VECTORS)
def test_known_vectors(password, salt_hex, iterations, expected):
    record = pw.hash_password(password, bytes.fromhex(salt_hex), iterations)
    assert record == f"{iterations}:{salt_hex}:{expected}"
    assert pw.verify(password, record)


def test_the_first_round_salts_and_the_rest_do_not():
    """
    The salt is mixed ONCE, into the first round, and later rounds hash only
    the previous digest. Getting that wrong on either side still produces a
    stable, plausible-looking hash that simply never matches the other
    implementation, which is the failure this pins.
    """
    salt = bytes.fromhex("aabbccdd00112233")
    record = pw.hash_password("pw", salt, 3)
    h = hashlib.sha256(salt + b"pw").digest()
    h = hashlib.sha256(h).digest()
    h = hashlib.sha256(h).digest()
    assert record.split(":")[2] == h.hex()


def test_a_password_verifies_against_its_own_record():
    record = pw.hash_password("correct horse battery staple")
    assert pw.verify("correct horse battery staple", record)
    assert not pw.verify("Correct horse battery staple", record)
    assert not pw.verify("", record)


def test_the_same_password_hashes_differently_each_time():
    """Random salt, so two devices given the same password share no record."""
    a = pw.hash_password("same")
    b = pw.hash_password("same")
    assert a != b
    assert pw.verify("same", a) and pw.verify("same", b)


def test_an_empty_password_is_refused_rather_than_hashed():
    """
    Empty means "no password" everywhere else, so hashing one would produce a
    record that looks like a password in force and can never be entered.
    """
    with pytest.raises(ValueError):
        pw.hash_password("")


@pytest.mark.parametrize("bad", [
    "", "   ", "notarecord", "1:xyz:abc", "1:0011:short",
    "0:0011223344556677:" + "aa" * 32,          # zero iterations
    "1::" + "aa" * 32,                          # no salt
    "1:0011223344556677:" + "aa" * 31,          # truncated digest
])
def test_unusable_records_are_not_a_password_in_force(bad):
    """
    A record we cannot parse must read as NO password, never as one nobody can
    satisfy. The device would otherwise refuse every login with no way back
    except a reflash, on the strength of a corrupt string.
    """
    assert pw.parse(bad.strip()) is None
    assert not pw.is_set(bad)
    assert not pw.verify("anything", bad)


def test_is_set_distinguishes_absent_from_present():
    assert not pw.is_set(None)
    assert not pw.is_set("")
    assert pw.is_set(pw.hash_password("x"))


def test_the_record_carries_its_own_iteration_count():
    """
    Self-describing, so raising ITERATIONS later leaves fielded devices working
    against the record they already hold rather than locking them out until
    they are next pushed to.
    """
    record = pw.hash_password("x", iterations=7)
    assert record.startswith("7:")
    assert pw.parse(record)[0] == 7
    assert pw.verify("x", record)
