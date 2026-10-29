Reference fixtures from libsbc's `sbcenc`/`sbcdec` (Debian bookworm
`sbc-tools` 2.0), made by `gen.sh`; cases and flags are in `cases.txt`.

- `in_*.au` input, `ref_*.sbc` libsbc's frames, `refdec_*.au` libsbc's decode
  of them: our decoder must reproduce `refdec` (>= 60 dB; measured ~75 dB, the
  gap is libsbc's fixed point) and our CRC/frame length must match every frame.
- `ours_*.sbc` our encoder's frames, `oursdec_*.au` libsbc's decode of them:
  must match the input as well as libsbc's own encoding does (within 1 dB;
  measured within 0.1 dB on all five cases), at unity gain and the spec delay.

Rerun `gen.sh` after changing the encoder.
