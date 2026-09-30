package sendspin

// Sendspin volume is 0-100. A device with its own volume scale maps it with
// these, which round-trip exactly for any scale of at least 100 steps: a
// server's volume command must come back as the same number, or the two ends
// correct each other forever.

// PercentToLevel maps 0-100 onto 0..max.
func PercentToLevel(pct, max int) int { return (clamp(pct, 0, 100)*max + 50) / 100 }

// LevelToPercent maps 0..max onto 0-100.
func LevelToPercent(level, max int) int { return (clamp(level, 0, max)*100 + max/2) / max }
