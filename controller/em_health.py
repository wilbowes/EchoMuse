"""
How a device's boot-time health reads to a person: eMMC wear and boot reason.

The device reports the eMMC's own EXT_CSD bytes (JEDEC JESD84-B51) and the
bootloader's boot reason on its register message; db.record_boot keeps one row
per boot. This turns a row into a dashboard line and a colour. Pure, so the
edges of the spec are tested without a database or a device.
"""
from __future__ import annotations

# EXT_CSD_REV below 7 (eMMC 5.0) predates the health fields: the bytes exist
# and mean nothing, which is not the same as "healthy".
HEALTH_MIN_REV = 7

# PRE_EOL_INFO [267].
_PRE_EOL = {1: "normal", 2: "warning", 3: "urgent"}

# Boot reasons that mean the previous boot did not end on purpose. Matched as
# substrings: the bootloader's spellings vary (wdt_by_pass_pwk, kernel_panic).
_UNPLANNED = ("wdt", "watchdog", "panic", "crash", "thermal")


def life_label(est: int | None) -> str | None:
    """DEVICE_LIFE_TIME_EST [268/269]: 0x01-0x0A in 10% steps, 0x0B exceeded."""
    if not isinstance(est, int) or isinstance(est, bool):
        return None
    if 1 <= est <= 10:
        return f"{(est - 1) * 10}–{est * 10}% used"
    if est == 11:
        return "past rated life"
    return None  # 0 is "not defined"; anything else is reserved


def emmc_summary(row: dict | None) -> dict | None:
    """
    {text, level} for the dashboard, or None when nothing was reported.

    The two life estimates cover different memory types; the worse one is
    the device's. level is ok / warn / error, error for urgent pre-EOL or a
    life estimate past rated.
    """
    if not row or row.get("emmc_rev") is None:
        return None
    if row["emmc_rev"] < HEALTH_MIN_REV:
        return {"text": "not reported by this part", "level": None}
    lives = [v for v in (row.get("emmc_life_a"), row.get("emmc_life_b"))
             if isinstance(v, int) and 1 <= v <= 11]
    worst = max(lives) if lives else None
    eol = row.get("emmc_pre_eol")
    eol_text = _PRE_EOL.get(eol)
    parts = [p for p in (life_label(worst), eol_text) if p]
    if not parts:
        return {"text": "unknown", "level": None}
    if eol == 3 or worst == 11:
        level = "error"
    elif eol == 2 or (worst is not None and worst >= 9):
        level = "warn"
    else:
        level = "ok"
    return {"text": " · ".join(parts), "level": level}


def boot_summary(reason: str | None) -> dict | None:
    """{text, level}: an unplanned boot (watchdog, panic) is a warning."""
    if not reason:
        return None
    low = reason.lower()
    level = "warn" if any(k in low for k in _UNPLANNED) else "ok"
    return {"text": reason, "level": level}


def wear_values(emmc) -> tuple | None:
    """The device's eMMC report as the stored columns, or None if it carries
    nothing usable. Anything not of the expected type stores as NULL."""
    if not isinstance(emmc, dict):
        return None

    def num(k):
        v = emmc.get(k)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def txt(k):
        v = emmc.get(k)
        return v if isinstance(v, str) and v else None

    vals = (num("rev"), num("preEol"), num("lifeA"), num("lifeB"),
            txt("name"), txt("date"), txt("manfid"))
    return vals if any(v is not None for v in vals) else None
