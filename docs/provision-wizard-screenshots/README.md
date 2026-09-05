# Provision wizard screenshots

Before/after captures of the provision wizard (#103), rendered from the
dashboard with the wizard opened at each touched step. "Before" is the
pre-PR base (`b167f85`); "after" is this branch.

Pairs (same step, before vs after):

| Before | After | Step |
|--------|-------|------|
| before-connect.png        | after-connect.png        | Connect Device (step 1) |
| before-wifi.png           | after-wifi.png           | Configure WiFi (step 11) |
| before-wifi-failure.png   | after-wifi-failure.png   | Configure WiFi, failed state |
| before-magisk-failure.png | after-magisk-failure.png | Install Magisk, failed state |
| before-install-em.png     | after-install-em.png     | Install EchoMuse (step 12) |

The failure states show the recovery panel (This step failed — fix the input
above and retry) and the per-step error markers; the before captures show the
same states with only flat text and no recovery affordance.
