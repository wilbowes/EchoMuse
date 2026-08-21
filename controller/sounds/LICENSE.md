# Bundled sounds

`timer_finished.flac` is taken unmodified from the Home Assistant Voice
Preview Edition sound set, so a finished timer sounds like the one people
already know from HA's own hardware rather than a tone this project invented.

[Home Assistant Voice Preview Edition Sounds](https://github.com/esphome/home-assistant-voice-pe/tree/dev/sounds)
© 2024 by [Clayton Charles Tapp](https://www.cctaudio.com/) is licensed under
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).

CC BY 4.0 permits redistribution, including in this project, provided the
attribution above travels with it — which is why this file sits beside the
audio rather than only in a commit message.

There is no fallback sound: if the audio is missing or cannot be decoded the
controller logs an error and refuses to ring, so this file is load-bearing and
must ship wherever the controller runs (see `COPY sounds/` in the Dockerfile).
