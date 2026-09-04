# Changelog

## 2.23.0-ea.3 (Early Access)

**Your Echoes will know what time it is, and updates stop stalling on things
they cannot do.** Nothing to do before updating: no database migration, nothing
to change on your devices. The clock needs firmware newer than v2.14.0 at both
ends.

### Echoes now know the time

An Echo has no clock that survives being unplugged, so it starts up believing
it is 2010 and only corrects itself if it can reach a time server. The
controller now simply tells it, over the connection it already has. Device log
timestamps line up with the controller's from the first moment, which is the
difference between a readable support bundle and a puzzle.

**Needs firmware newer than v2.14.0.**

### Updates no longer stall for two minutes at a time

A file transfer to a folder that does not exist on the device used to wait out
its full two-minute timeout instead of failing immediately — and it held that
device's connection for the whole time, so whatever came next failed too. One
firmware update could lose four minutes to this and report a confusing error
about something unrelated. Transfers now check first and fail straight away.

A related fault could make the controller close a connection belonging to a
transfer that was still using it, which is what turned a stalled transfer into
an error message pointing somewhere else entirely.

### Maintenance actions that do not apply are now greyed out

The Re-apply debloat button is disabled, with the reason shown, on an Echo that
is not running Android — there are no Amazon packages to hide there, and
pressing it achieved nothing while tying the device up.

### Smaller things

- The ambient light sensor is read less often. Its driver logs a line every
  time it reads in a dark room, which was filling the small area the Echo keeps
  crash reports in — so a crash overnight could no longer be explained. Nothing
  visible changes.

## 2.23.0-ea.2 (Early Access)

**Sound through the headphone jack works properly, and the controller stops
spending minutes sending maintenance files an Echo cannot use.** Nothing to do
before updating: no database migration, nothing to change on your devices. The
jack fixes need firmware newer than v2.14.0 and do nothing until you have it.

### The headphone and line-out jack

Plugging a speaker or headphones into the Echo produced almost no sound. The
jack has its own output stage, and inserting a cable drops it to the bottom of
its range — nothing on our side ever raised it again, so the audio was present
and inaudible. It is now set whenever a cable is detected.

Booting with a cable already plugged in was the same gap from the other
direction. The Echo only corrected its audio routing when a cable was inserted
or removed, and a device that started up with one connected never had such a
moment — so it played to the room with a cable attached. Unplugging and
replugging was the folk remedy for both, and is no longer needed.

**Needs firmware newer than v2.14.0.**

### The controller knows what each Echo is running

Echoes now report which base system they booted, and the controller sends
Android-specific maintenance files only to the ones actually running Android.
Elsewhere each attempt sat for two minutes before giving up, which made an
ordinary firmware update look as though it had stalled when the update itself
had already finished.

### Smaller things

- The Local Build file picker clears itself once a deploy starts, rather than
  leaving a filename sitting there as though something were still pending.

## 2.23.0-ea.1 (Early Access)

**The Bluetooth proxy stops crowding out the device it runs on, and the
controller stops trusting a device to be up to date.** Nothing to do before
updating: no database migration, nothing to change on your devices. Two of the
changes below need firmware newer than v2.14.0 and do nothing until you have
it — they are harmless without it.

### The Bluetooth proxy no longer competes with its own device's health

An Echo running the Bluetooth proxy sent every advertisement it heard over the
same connection the controller uses to check the device is alive. Bulk
telemetry and the liveness check took turns on one channel, so a busy room made
the device look unwell — measured at 3615 round-trip delays in a day against 2
on the Echo beside it, and the fault followed the proxy when it was moved.

Advertisements now ride the data connection instead. **This needs firmware past
v2.14.0 at both ends**, and the two halves agree before either uses the new
path, so an older device keeps working exactly as before rather than silently
dropping advertisements.

### Firmware updates no longer slow down whoever is talking

Updating several Echoes at once stalled the controller for as long as eleven
seconds, and that is the same loop that sends audio and ring animations — so an
Echo answering someone paid for an Echo being updated. Updates now run one at a
time across the whole controller, queued rather than refused.

### Only one Echo answers when you interrupt

Interrupting a response in a room with more than one Echo could start a turn on
each of them. The same arbitration that already decides which Echo answers a
wake word now decides which one takes an interruption.

### The ring shows whether the Echo can reach the controller

An Echo that has lost the controller now says so on its ring rather than
looking idle, and its buttons stand down instead of appearing to work. **Needs
firmware past v2.14.0.**

### Devices are checked against what they actually have

The controller assumed a device already held its wake word models, its startup
script and its debloat list, and only ever verified the first — and only when
the device was scoring wake words itself. One of our own Echoes ran for a
fortnight missing three of its four wake word models while every panel called
it healthy. All three are now checked when a device connects.

### Installing firmware an Echo already has

Pushing a build an Echo is already running cost it a reboot and a slot for no
change, and nothing stopped it. Updating one Echo now says so and refuses;
updating the whole fleet skips the ones already on that version, which it did
for published releases but never for a binary you uploaded yourself. You can
still force it — writing the same version again is how a damaged slot gets
repaired.

### Smaller things

Request logging is quiet by default, so the log is about your devices rather
than about the dashboard polling itself — thank you to @DennisGaida. Security
and dependency updates for websockets, cryptography and protobuf.

## 2.22.0

**Timers, and your Echoes can now be asked a question.** Everything from the ten
2.22.0 Early Access builds.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices.

### Timers ring on the Echo

Ask an Echo to set a timer and it rings on that Echo, with the same alert sound
Home Assistant's own Voice PE hardware uses. Say "stop" — or press the button on
top — and it stops. Saying something that merely contains the word "stop", like
"stop the music", still does what you asked and still gets its answer.

Stopping a ringing timer used to leave the Echo deaf. Dismiss it while the alarm
was actually sounding, rather than in the pause between chimes, and the Echo
went quiet as it should and then stopped responding to its wake word entirely,
until the controller was restarted. The chime sounds for most of each cycle, so
this caught roughly three dismissals in four.

### Home Assistant can ask a question and wait for the answer

`assist_satellite.start_conversation` ("the garage door is open, want me to close
it?") and `assist_satellite.ask_question` now work. EchoMuse devices never
appeared as eligible targets for either, so those actions could not be pointed at
them at all.

The Echo plays the message, then its ring lights and it listens, exactly as after
a wake word. An attention chime plays first — Home Assistant sends one and we
were discarding it, and it matters more here, since an unprompted question
otherwise arrives with no warning.

A muted Echo will not open its microphone, and nothing here weakens that. The
question is still spoken; the answer is simply never heard.

Reported by @pollocluck (#396), with the root cause found by @vbtheory (#335).

### Long answers no longer cut off part-way through

Ask for something that takes a minute to say and the Echo would go quiet
mid-sentence, then sit there looking like it was still speaking. Short answers
were always fine, which made it look like a network problem that came and went.

It was not the network. The controller was sending the whole answer as fast as
the connection would carry it — around 35 seconds of speech pushed across in 21 —
and the Echo can only hold about five and a half seconds ahead of what it is
playing. While it worked through the backlog it stopped answering the
controller's "are you still there?" checks, and after ten seconds the controller
hung up. The answer is now sent four seconds ahead of what is playing and no
further. Nothing is lost by slowing down; everything beyond that was queueing on
the network rather than reaching the speaker.

### An Echo with nothing behind it now says so

Say the wake word while Home Assistant is not connected to that Echo and the ring
lit for a fraction of a second and went out. The Echo had heard you perfectly and
had nowhere to send it, but from across the room it looked like a device that had
failed to notice you.

It now holds the listening ring briefly and flashes orange twice — the colour it
already uses when it cannot find its controller, read one step further along. The
button does the same, being the control people reach for when the wake word seems
to have done nothing.

This happens more often than it sounds: an Echo comes back from a restart in well
under a minute and Home Assistant can take another minute to reconnect to it.
Measured on our own hardware, fifty-six seconds.

With several Echoes, one with no Home Assistant behind it could also win the
utterance, silence the one that was ready and then fail. It now steps aside as
soon as it hears the wake word, before deciding which Echo answers. The wake
still appears in the device's activity history, so an outage remains visible
afterwards.

### The ring says it has stopped listening

Start a turn with the button and the ring kept its listening animation from the
press until the answer began — often ten seconds — with nothing to say the Echo
had heard you stop. It made the Echo feel slow to react when it had finished
listening at the usual time. A turn can end two ways, and only one of them
switched the ring to its thinking spinner. Both do now.

### Deleting an Echo removes it

A deleted Echo carried on working. It vanished from the dashboard and kept
serving conversations, kept its Home Assistant port and kept listening, only
coming back as a new device when something else interrupted its connection. So
"delete it and add it again" worked eventually, or never, depending on the
weather. Adding it back had a second fault behind the first: the new entry
inherited the port the old one had been deleted to move off, so the Status panel
showed no voice port and the Bluetooth proxy panel disappeared with it.

### Music, announcements and network blips

- Music no longer comes back to full volume mid-answer when an announcement lands
  while the Echo is already speaking.
- Music asked for during a reply no longer plays over the reply, and a request
  made during a turn survives instead of being dropped.
- A four-second network interruption — a controller restart, an add-on update, a
  moment of bad wifi — no longer deregisters the Echo's entities, drops its
  Bluetooth proxy and stops what it was playing. It now waits to see whether the
  device comes back first.
- A struggling connection no longer makes the controller rebuild a microphone
  that was working.
- Interrupting the Echo mid-response no longer leaves an error and a traceback in
  the log, and can no longer leave the turn open.

### Dashboard and support

- The Status panel has a **Voice assistant** row, so an Echo Home Assistant has
  never connected to no longer looks exactly like one that works.
- An expired dashboard session returns you to the sign-in page instead of quietly
  asking for the device list forever. One tab left open overnight made 1262
  refused requests.
- Announcements no longer raise an error inside Home Assistant.
- Support bundles reach substantially further back, by summarising the network
  timing blips that made up half of one.
- The approval step for a newly connected device is now a labelled prompt rather
  than a tab that did not look like one.

### One setting for newer firmware

**Config → Microphones → Advanced** gains an **Echo reference** control, for
choosing where echo cancellation takes its copy of what the Echo is playing.
Leave it on **Auto** unless you are deliberately measuring the difference.

It needs firmware newer than v2.13.0, **which is not published yet** — v2.13.0 is
the current release. Older Echoes ignore the setting entirely and carry on as
they always have.

### Known issue: the Bluetooth proxy

An Echo running the Bluetooth proxy sees brief reconnects — a few a day on our
own hardware — because the advertisements it forwards share a connection with the
messages that check the Echo is still there. Home Assistant shows the satellite
drop out and come back within seconds. It is being worked on and tracked in #404;
nothing in this release makes it worse, and turning the proxy off removes it
entirely.

## 2.22.0-ea.10 (Early Access)

**A fix for interrupting your Echo mid-response.** Nothing to do before
updating: no database migration, no firmware requirement, nothing to change on
your devices.

### Talking over a response cleaned up badly

When you interrupt an Echo while it is speaking, the controller stops decoding
the rest of the response and tears down the audio pipeline. It was doing that
in the wrong order, and two things followed.

The visible one was noise in the log: an unhandled error with a full traceback,
printed on every interruption. Harmless in itself, but it sat next to whatever
someone was actually investigating and cost them time — and it goes into support
bundles.

The other was not visible and matters more. In the right conditions the teardown
could stop making progress rather than finish, holding the turn open. We have
not seen this happen on a real device, so this is a fault found by measurement
rather than one reported from the field; the fix removes the possibility either
way.

Nothing about interrupting changes from your side — it behaves as it did, minus
the error.

## 2.22.0-ea.9 (Early Access)

**Your Echoes can now be asked a question by Home Assistant, and will listen for
the answer.** Nothing to do before updating: no database migration, no firmware
requirement, nothing to change on your devices.

### Announce, then listen

Home Assistant has two actions that speak to a satellite and then wait for a
spoken reply — `assist_satellite.start_conversation` ("the garage door is open,
want me to close it?") and `assist_satellite.ask_question`, which matches what
you say against a list of answers you supply. EchoMuse devices never appeared as
eligible targets for either, so the actions could not be pointed at them at all.

They do now. Both work the same way from your side: the Echo plays the message,
then its ring lights and it listens, exactly as it does after a wake word. An
attention chime plays before the message — Home Assistant sends one, and we were
discarding it. It matters more here than for an ordinary announcement, since an
unprompted question otherwise arrives with no warning.

Two things worth knowing:

- **The Echo must have a microphone and be connected.** A device Home Assistant
  cannot reach is not offered as a target.
- **A muted Echo will not open its microphone**, and nothing in this change
  weakens that. The question is still spoken; the answer is simply never heard,
  and the action reports back that it got no answer.

Reported by @pollocluck (#396), with the root cause found by @vbtheory (#335) —
both halves of it, correctly, before either had been looked at here.

## 2.22.0-ea.8 (Early Access)

**Adds one setting, for testing echo cancellation.** Nothing else changes, and
there is nothing to do before updating.

### Echo reference (Config → Microphones → Advanced)

Echo cancellation needs a copy of what the Echo is playing, so it can subtract
it from what the microphones hear. There are two places that copy can come
from, and newer firmware can use either:

- **Auto** (default, and what you already have) — use the copy the audio chip
  provides, and fall back to the software one if the chip does not offer it
- **Hardware** / **Software tap** — pin one, to compare them

Leave it on **Auto** unless you are deliberately measuring the difference.
Pinning **Hardware** on an Echo whose chip does not provide that copy means no
echo cancellation at all.

**Needs firmware newer than v2.13.0.** Older Echoes ignore this setting
entirely and carry on as they always have — nothing breaks, the setting simply
has no effect.

## 2.22.0-ea.7 (Early Access)

**Long spoken answers no longer cut off part-way through.** That is the whole
release, and it has been happening for weeks.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices.

### Why a long answer stopped mid-sentence

Ask for something that takes a minute to say and the Echo would go quiet
part-way through, then sit there looking like it was still speaking. Short
answers were always fine, which made it look like a network problem that came
and went.

It was not the network. The controller was sending the whole answer to the Echo
as fast as the connection would carry it — around 35 seconds of speech pushed
across in 21. The Echo can only hold about five and a half seconds of audio
ahead of what it is playing, so the rest piled up, and while the Echo was busy
working through the backlog it stopped answering the controller's "are you
still there?" checks. After ten seconds without an answer the controller
assumed it had lost the Echo and hung up — mid-sentence.

The controller now sends the answer four seconds ahead of what is playing and
no further. Nothing is lost by slowing down: the Echo could never hold more
than five and a half seconds anyway, so everything sent beyond that was
queueing on the network rather than reaching the speaker. Music has worked this
way since July; spoken answers simply never got the same treatment.

You should notice nothing except that long answers now finish.

### An Echo stopped listening for up to 40 seconds after a network blip

If the audio connection dropped while the main connection stayed up, the Echo
would stop listening and not start again until the controller eventually
noticed — measured at 34 and 41 seconds on our own test device. It is now about
five, because the Echo restarts listening itself as soon as it reconnects
instead of waiting to be told. The same fix closes a gap at start-up where an
Echo could sit not listening for around 40 seconds after booting.

**This part needs firmware.** It is a device change, and device firmware ships
separately from the controller — an Echo running v2.13.0 or earlier still has
the old behaviour.

### Better answers when something does go wrong

Two diagnostic changes, both invisible unless you go looking:

- When a connection to an Echo closes, the log now says **why**, and which side
  hung up. Every drop used to log "connection closed" and nothing else, which
  is what made the problem above take so long to find.
- Bluetooth proxy resets are now recorded. On these Echos, Bluetooth and Wi-Fi
  share one radio, so a Bluetooth hiccup can take the network with it. If you
  use the Bluetooth proxy and see an Echo drop off, the Bluetooth panel now
  shows whether the radio restarted around then.

## 2.22.0-ea.6 (Early Access)

**An Echo with no Home Assistant connection now says so every time you speak to
it.** One fix, correcting ea.5.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices.

### The orange flash did not appear if another Echo answered

ea.5 added an orange double flash for an Echo that hears you with no Home
Assistant behind it. It only appeared when that Echo was the one running the
turn — so on a house with more than one, the flash vanished exactly when it was
most needed. Stand in front of the disconnected Echo, say the wake word, and if
a second Echo elsewhere took the utterance, the one in front of you lit its ring
and went dark with no explanation, while a different room answered.

The flash is not a report on the turn. It is the Echo telling you three things
about itself: the wake word is working, the controller is connected, and Home
Assistant is not. None of that depends on what any other Echo did, so it now
shows every time, and still clears itself after a second rather than sitting
there lit.

The button does the same. It is the control people reach for when the wake word
seems to have done nothing, so it was the worst one to answer with silence.

### An Echo without Home Assistant no longer takes the turn at all

It now steps aside as soon as it hears the wake word, before deciding which Echo
answers, rather than claiming the utterance and failing a moment later. On a
single-Echo setup you will see no difference beyond the flash. On several, it
means a disconnected Echo can no longer take an answer away from one that was
ready — including when it is the nearer of the two, which is the case that used
to lose you the reply entirely.

The wake still appears in the device's activity history, so an outage remains
visible afterwards rather than looking like an Echo that never heard anything.

## 2.22.0-ea.5 (Early Access)

**The Echo now tells you when there is nothing to answer you.** Two fixes, both
about what happens when Home Assistant is not connected.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices.

### The ring went dark instead of saying Home Assistant was missing

Say the wake word — or press the button — while Home Assistant is not connected
to that Echo, and the ring lit for a fraction of a second and went out. The Echo
had heard you perfectly and had nowhere to send it, but from across the room it
looked like a device that had failed to notice you at all.

It now holds the listening ring briefly, to say it heard you, and then flashes
orange twice. Orange is the colour the Echo already uses when it cannot find its
controller, so it reads the same way one step further along: the problem is not
this device, it is what sits above it.

This happens more often than it sounds. An Echo comes back from a restart or a
power cut in well under a minute, but Home Assistant can take another minute to
reconnect to it — and in that gap the wake word works, the microphone works, and
nothing can answer. Measured on our own hardware this week: fifty-six seconds
between the Echo being ready and Home Assistant arriving.

### With several Echoes, the one that could answer stood down

When one utterance wakes more than one Echo, the first to hear it answers and
the others stay quiet — otherwise they all reply at once. But "first to hear it"
is about which one you are standing nearest, and that has nothing to do with
whether Home Assistant is connected to it. So an Echo with no Home Assistant
behind it could win, silence the one that was ready, and then fail.

The Echoes Home Assistant is not connected to no longer take that decision away
from the ones it is.

## 2.22.0-ea.4 (Early Access)

**The ring now tells you it has stopped listening, whichever way you started
the turn.** One fix.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices.

### The ring stays on "listening" after you have finished speaking

Start a turn with the button rather than the wake word and the ring kept its
listening animation from the moment you pressed it until the answer began —
often ten seconds or more — with nothing to say the Echo had heard you stop.
It made the Echo feel slow to react when it had in fact finished listening at
the usual time, within about three seconds.

A turn can finish in two ways: Home Assistant deciding you have stopped
speaking, or the Echo deciding it for itself. Only the first switched the ring
to its thinking spinner. Both do now.

**This was never really about the button.** Which of the two gets there first
is a matter of timing, and a wake word turn on a slow network could lose the
same feedback. The fix applies to both, so it cannot come back on the other
one.

The time between finishing speaking and hearing a reply is unchanged — that is
mostly speech-to-text, and it depends on the machine running Home Assistant.

## 2.22.0-ea.3 (Early Access)

**Stopping a ringing timer no longer leaves the Echo deaf.** One fix, and it
is worth taking straight away if you use timers at all.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices.

### Stopping a timer while it is chiming

Press the button, or say stop, while the alarm was actually sounding — rather
than in the pause between chimes — and the Echo went quiet as it should, but
then stopped responding to its wake word entirely. Saying the wake word lit the
ring for a while and did nothing else. The dashboard went on showing the Echo
as **Speaking** the whole time. It stayed that way until the controller was
restarted.

The chime sounds for most of each cycle, so this caught roughly three
dismissals in four, which is why it looked occasional rather than reliable.

If you are on an affected version and it happens, pressing the button once more
recovers the Echo — that takes a different path and clears the stuck state.

## 2.22.0-ea.2 (Early Access)

**Deleting an Echo now actually removes it, and the dashboard stops claiming
things are fine when they are not.** Everything here was found on a running
Early Access controller rather than in testing, which is the point of the
channel.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices. Your Echoes keep their Home
Assistant entities.

### Deleting an Echo removes it

A deleted Echo carried on working. It vanished from the dashboard and kept
serving conversations, kept its Home Assistant port and kept listening for the
wake word, only coming back as a new device when something else interrupted
its connection — a reboot, a controller restart, a moment of bad wifi. So
"delete it and add it again" worked eventually, or never, depending on the
weather.

Adding it back had a second fault behind the first. The new entry inherited
the port the old one had been deleted to move off, so the Status panel showed
no voice port at all and the Bluetooth proxy panel disappeared with it. One
cause, two panels, neither pointing at it.

### The dashboard says whether Home Assistant is connected

An Echo that Home Assistant has never connected to looked exactly like one
that works: **Online, idle**. Nothing on the page distinguished them, so the
usual cause — a stale Home Assistant entry after a device was re-added —
looked like the Echo was broken.

The Status panel now has a **Voice assistant** row that reads the same thing
the conversation itself reads, so it cannot disagree with what actually
happens:

```
Voice assistant     HA connected · port 16003
Voice assistant     Waiting for HA · port 16003
```

### Announcements no longer throw an error in Home Assistant

Every announcement the Echo made raised an error inside Home Assistant, with
nothing visible to show for it. The Echo was reporting a playback state Home
Assistant's ESPHome integration has no name for. It now reports one it does.

### A dashboard tab left open no longer asks forever

When a dashboard session expired, the page kept asking for the device list
every five seconds and quietly discarding the refusal. The list simply stopped
updating, which reads as a controller that has stopped responding, and there
was no way to tell from the page that you had been signed out. One tab left
open overnight made 1262 refused requests.

An expired session now returns you to the sign-in page.

### Support bundles reach back further

Roughly half of a support bundle's log was one measurement, repeated: a line
for every network timing blip on every Echo, thousands of them, when the same
figures are already recorded properly with a total to compare them against.
Blips that actually delayed audio are still logged one by one; the rest are
summarised.

Combined with the fix above, a bundle now covers substantially more of the
period before the problem you are reporting.

## 2.22.0-ea.1 (Early Access)

**Timers.** Ask the Echo to set one and it rings on the Echo itself, with the
same alert sound Home Assistant's own Voice PE hardware uses. Say "stop" — or
press the button on top — and it stops. Saying something that merely contains
the word "stop", like "stop the music", still does what you asked and still
gets its answer.

Alongside it, four fixes for things that went wrong when two parts of the
Echo wanted the speaker at once, or when your network hiccuped.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices.

### Music no longer comes back to full volume mid-answer

Music ducks under the Echo's voice and lifts again when it has finished. If
two things were speaking — an announcement arriving while the Echo was already
answering you — whichever finished first put the music back up, and the rest of
the answer competed with it.

### Music asked for during a reply no longer plays over the reply

"Play some jazz" gets acted on by Home Assistant before the spoken confirmation
has been generated, so the music can arrive while the Echo is still talking.
That was already handled — but an announcement landing in the middle released
the hold early, and the music started underneath the answer. A request you made
during a turn also survives now, instead of being quietly dropped if an
announcement happened to land after it.

### A brief network blip no longer removes the Echo from Home Assistant

A four-second interruption — a controller restart, an add-on update, a moment
of bad wifi — used to deregister the Echo's entities, drop its Bluetooth proxy
and stop whatever it was playing, then rebuild all of it when the device came
back seconds later. It now waits to see whether the device returns first.

### A struggling connection no longer makes the Echo restart its microphone

When no audio arrived, the controller assumed the microphone had stopped and
began rebuilding it — even when the reason was simply that the connection
carrying the audio had dropped. It now checks. A microphone that really has
stopped is still repaired, exactly as before.

### The Echo stops listening once Home Assistant has heard you

In a room with background noise the Echo could keep listening well after you
had finished, holding back an answer that was already written. It now stops as
soon as Home Assistant has the words.

### Setting up a new Echo is clearer

The approval step for a newly connected device was a single tab that did not
look like one, so it was easy to miss entirely. It is now a labelled prompt.

### Two quieter ones

Two media requests arriving within a second of each other could kill playback
for both. And a device that dropped off mid-stream wrote one warning per audio
frame — thousands of identical lines that pushed everything explaining the
fault out of the support bundle.

## 2.21.0

**The Echo stops throwing away the question you just asked.** Three separate
faults could each lose a turn, and the worst of them made asking again the one
thing guaranteed not to work. Alongside them: every Echo now knows all four
wake words, noise suppression no longer deletes quiet speech, a single bad
message can no longer drop a device off Home Assistant, and the Activity tab
tells you how a turn ended rather than lumping every interruption together.

**There is nothing to do before updating.** No database migration, no firmware
requirement, nothing to change on your devices. One improvement waits on a
firmware release that is not out yet, and says so where it appears.

### Asking again after a turn that went nowhere no longer gets ignored

If Home Assistant took too long to answer, the turn gave up after 30 seconds —
and then so did the next one, and the one after that, each in about three
milliseconds with nothing recorded but a refusal.

Giving up left Home Assistant's side of the conversation still running. Its
eventual "run finished" message arrived while the *next* question was
starting, and that question was thrown away as though it had already ended.
The natural thing to do — ask again — was the one thing that could not work.
Every refusal in a 15-hour sample followed a slow answer; none followed a
successful one.

The same applies to the quieter version: saying the wake word and then not
speaking left the conversation open in the same way. Any turn that ends
without Home Assistant finishing its side now closes that side properly,
whatever the reason it ended.

**The slow answers themselves are a Home Assistant problem and are not fixed
here.** What changes is that one slow answer now costs one turn instead of
three.

### The Echo no longer interrupts itself while it is thinking

Ask for something, and while the Echo was working out what you meant it could
decide you had spoken again — cancel what you actually asked for and reopen
the microphone at you. Nothing you said was sent anywhere. It looked like the
Echo had lost interest and started listening for no reason.

It was reacting to room noise. While thinking, the bar for "they're talking
over me" sat low enough that ordinary background sound could clear it twice in
a row, and twice in a row was enough. That low bar was added to catch a real
case, and the reason it was needed has since been fixed independently.

Interrupting the Echo while it thinks still works exactly as before, because
saying the wake word out loud scores far above anything a room does.

### Responses no longer cut off at the last word

About one response in nine ended a fraction of a second early and was recorded
as a failure, having played almost to the end. Short replies lost their last
syllable, and nothing in the log said why.

The final fragment of audio is padded out to a whole block before it is sent.
The loudness limiter holds a few milliseconds back, and when that held audio
was handed over it could push the fragment past a whole block — at which point
the padding was a negative length and the response stopped there.

### The Echo that did not answer stops sitting there lit

With more than one Echo in earshot, both wake up and one answers. The other
now goes dark immediately instead of holding its ring lit for up to 30
seconds.

Each Echo lights its own ring the instant it hears the wake word, which is
what makes the response feel immediate — and it means a device lights up
before it can know whether it is the one answering. Nothing told the losing
device to stop, so it sat lit until an unrelated timeout expired.

### Every Echo now knows all four wake words

Your Dot was given the recogniser for whichever wake word was selected when it
was provisioned. Choose a different one later and it needed that recogniser
copied across first — which normally happened, but left a gap: a device that
was **offline** when you changed the wake word was told to listen for a word it
did not have. On a device doing its own wake word detection, that is a Dot that
hears nothing. Nothing warned, and the dashboard showed it as healthy.

All four stock wake words (Hey Jarvis, Alexa, Hey Mycroft, Hey Rhasspy) are now
installed on every device — 3 MB in total — so switching between them is
instant and cannot fail. Devices you provisioned earlier collect the missing
ones automatically the next time they connect, and any device found without the
recogniser it was told to use falls back to the controller listening on its
behalf, rather than going quietly deaf.

Custom wake words you have trained yourself are untouched and are still copied
across on selection.

### Noise suppression no longer swallows quiet words

**Noise suppression** could cut speech to complete silence rather than just
turning it down. Measured on real turns: with it on, 8–15% of samples at a
healthy speaking level were digital silence, against 0.3% with it off. That is
the difference between a word sounding muffled and a word not being there at
all, and it lands hardest on exactly the quiet speech the setting is meant to
rescue.

Suppression is now limited to 20 dB. A passage the denoiser judges to be noise
is pushed well down instead of removed, and where it judges speech to be clean
it passes through untouched — so nothing that was working gets quieter. Speech
recognition stops improving well before 20 dB of noise reduction, so the limit
costs nothing that was being collected.

If you turned **Noise suppression** off because transcripts came back with
words missing, it is worth another try.

### A single bad message no longer drops the device

One malformed or unexpected control message could take a device's whole
connection down with it — the voice satellite, the Bluetooth proxy and the
audio channel together — and it reconnected every time it happened. The
message that triggered it in practice was a **playback statistics report**,
which is pure telemetry: the least important thing the device sends was able
to disconnect it.

Each message is now handled on its own. One that fails is logged with what it
was, and everything else carries on.

### The Activity tab says how a turn ended

Three different things stop a response, and until now they all recorded the
same way. They are now distinct:

| Outcome | What happened |
|---|---|
| **barged** | You said the wake word over it — a new turn followed |
| **cancelled** | You pressed the action button |
| **muted** | You muted the device, which also turns the microphone off |

Before this, a response you cut off mid-sentence was recorded as a *completed
answer*. A fleet interrupting a third of its responses read as a fleet
answering everything, which is how a real fault stayed hidden for two days.

**Expect your numbers to move.** Turns you interrupted on earlier builds were
counted as answered, and mutes during a response were counted as cancels.
Nothing about the fleet changed — only the counting. Older rows keep whatever
they were recorded as; nothing is rewritten, because a cause that was never
captured should not be invented later.

### Promoting a user no longer needs the API

The first person to sign in becomes admin and everyone after is read-only,
which was correct and had no way to change it short of a hand-written API
call. **Settings → Users** now lists every account with its role, shows which
are linked to a Home Assistant login, and promotes or demotes per row. Where
the server refuses — the last admin cannot be demoted — it says why.

Contributed by @chr-braun.

### Turning update checks off turns them off, and says so

Setting **Update check interval** to `0` was the obvious way to stop the
controller contacting GitHub, and it did the opposite: it removed the wait
between checks entirely, so the controller polled continuously until GitHub
rate-limited it. `0` now means off, and a value that is not a number falls back
to the hourly default rather than silently killing update checking altogether.

The Updates tab used to show "No release info" in that state, which looks
exactly like GitHub being unreachable — so the tab you would open to find out
what was wrong could not tell you. It now reads **Auto-checks off**. **Check
now** still works either way: pressing it is a deliberate request, not
background traffic.

### If you build your own firmware

Uploading your own compiled binary from **Updates → Local Build** failed with
"an internal error occurred" — the controller was capping uploads at 1 MB
against a firmware roughly ten times that, so the file never reached the code
meant to handle it. This broke on 2026-08-18 in 2.20.2 when a routine
dependency update changed how the web framework applies its size limit.
Updating from a published release was never affected. The limit is now 50 MB,
and a file over it says so, with its size.

A successful upload also used to report a rollback that had not happened
(`⚠ Device reconnected on v2.12.0-63-g99628d3 — auto-rolled back`). The
controller now recognises the version string a clean checkout produces, and a
mismatch reports what the device came back on and what was expected instead of
guessing. A genuine rollback still says so.

### Music playback

A Music Assistant stream that stopped producing audio — an upstream failure
rather than the end of a track — used to wait indefinitely, with Home Assistant
showing **playing** against silence and nothing in the log to say otherwise. A
source that produces nothing for 30 seconds now ends the stream, reports idle,
and logs what happened. A stream that is merely slow still recovers; the clock
resets on every chunk that arrives.

Audio fetched over `https` did not verify the server's certificate. It does
now. If you stream from a server with a private or self-signed certificate,
point `EM_EXTRA_CA_CERT` at your CA and it works as before. Playback failures
now include the decoder's own error message, which previously went nowhere.

### The listening ring lights immediately — needs firmware v2.13.0

On an Echo doing its own wake word detection, the ring waited for a round trip
to the controller before lighting: measured at half a second, and longer on a
busy network. It now lights the moment the device hears you.

**That firmware is not published yet.** On the firmware you are running today
everything behaves exactly as it does now, and the dashboard will offer the
update when it is released.

### Also

- Clipping caught while the limiter is switched off is the backstop doing its
  job on a boosted EQ, not a fault. It was counted alongside genuine faults, so
  a working setup looked broken. The two are now counted separately.
- The command-line tools in `controller/tools/` find the database under the
  Home Assistant add-on as well as the standalone container.
- The controller's event-loop stall figure read zero under the add-on however
  busy it got. The warnings in the log were always correct; the number beside
  them now agrees. Contributed by @chr-braun.

## 2.21.0-ea.7 (Early Access)

One fix on ea.6, for a fault that could throw your question away before
anything had heard it.

### The Echo no longer interrupts itself while it is thinking

Say the wake word, ask for something, and while the Echo was working out what
you meant it could decide you had spoken again — cancel what you actually
asked for, and reopen the microphone at you. Nothing you said was ever sent
anywhere. It simply looked like the Echo had lost interest and started
listening for no reason.

It was reacting to room noise. While thinking, the bar for "they're talking
over me" sat low enough that ordinary background sound could clear it twice in
a row, and twice in a row was enough.

That low bar was added to catch a real case, and the reason it was needed has
since been fixed independently. Nothing needed it any more, and what it caught
instead was the room. Interrupting the Echo while it thinks still works
exactly as before, because saying the wake word out loud scores far above
anything a room does.

## 2.21.0-ea.6 (Early Access)

Three fixes on ea.5, all found by reading a night of ea.5's own logs rather
than by anyone reporting them.

### Asking again after a turn that went nowhere no longer gets ignored

If Home Assistant took too long to answer, the turn gave up after 30 seconds —
and then so did the next one, and the one after that, each in about three
milliseconds with nothing recorded but a refusal.

Giving up left Home Assistant's side of the conversation still running. Its
eventual "run finished" message arrived while the *next* question was
starting, and that question was thrown away as though it had already ended.
The natural thing to do — ask again — was the one thing guaranteed not to
work.

Every refusal in a 15-hour sample followed a slow answer. None followed a
successful one.

This also covers the quieter version of the same thing: saying the wake word
and then not speaking left the conversation open in the same way, so the next
question could be swallowed by a turn you had already abandoned. Any turn that
ends without Home Assistant finishing its side now closes that side properly,
whatever the reason it ended.

**The slow answers themselves are a Home Assistant problem and are not fixed
here.** Each one was a request to play music on a named device where speech
recognition worked and no reply ever came back. What changes is that one slow
answer now costs one turn instead of three.

### Responses no longer occasionally cut off at the last word

About one response in nine ended a fraction of a second early and was recorded
as a failure, having played almost to the end.

The final fragment of audio is padded out to a whole block before it is sent.
The loudness limiter holds a few milliseconds of audio back, and when that
held audio was handed over it could push the fragment past a whole block —
at which point the padding was a negative length and the response stopped
there.

Short replies lost their last syllable. Nothing in the log said why, and the
turn was filed under errors.

### The Echo that did not answer stops sitting there lit

With more than one Echo in earshot, both wake up and one answers. The other
now goes dark immediately instead of holding its ring lit for up to 30
seconds.

Each Echo lights its own ring the instant it hears the wake word, which is
what makes the response feel immediate — but it means a device lights up
before it can know whether it is the one answering. Nothing told the losing
device to stop, so it sat lit until an unrelated timeout expired.

## 2.21.0-ea.5 (Early Access)

One change on ea.4, to what the Activity tab tells you.

### An interrupted answer now says how it was interrupted

Three different things stop a response, and until now they all recorded the
same way. They are now distinct:

| Outcome | What happened |
|---|---|
| **barged** | You said the wake word over it — a new turn followed |
| **cancelled** | You pressed the action button |
| **muted** | You muted the device, which also turns the microphone off |

ea.4 already recorded a wake word spoken over a *response* as **barged**. One
spoken while the Echo was still thinking recorded as **cancelled**, because
the two travel different paths internally and only one had been covered. Both
are barges and both now say so.

Recording all three the same way made a person talking over the assistant
indistinguishable from a button press. It also hid the fault this was built to
find: a device triggering on its own speech shows up as a barge followed by a
turn the barge started, and half of those were invisible.

**Expect your numbers to move.** Turns you interrupted on earlier builds were
counted as answered, and mutes during a response were counted as cancels.
Nothing about the fleet changed — only the counting. Older rows keep whatever
they were recorded as; nothing is rewritten, because a cause that was never
captured should not be invented later.

## 2.21.0-ea.4 (Early Access)

One fix on ea.3, for anyone who builds their own firmware.

### Deploying your own build no longer reports a rollback that did not happen

Uploading a locally compiled binary from **Updates → Local Build** worked, and
then said it had not:

```
⚠ Device reconnected on v2.12.0-63-g99628d3 — auto-rolled back
```

The device in that message is running exactly what was pushed. Nothing rolled
back.

Two causes. The controller reads the version out of the uploaded binary to
know what to expect on reboot, and it only recognised the older date-style
version string — so a build made from a clean checkout was labelled with a
placeholder the device could never report. And the dashboard treated any
mismatch as a rollback, when a rollback specifically means the device came
back on the firmware it had **before**.

Both fixed. A genuine rollback still says so. Anything else reports what the
device came back on and what was expected, without guessing, and explains the
placeholder when that was the cause.

Nothing here affects updating from a published release, which reads its
version from the release rather than from the binary.

## 2.21.0-ea.3 (Early Access)

One fix on ea.2, to a number you would otherwise have trusted.

### An answer you interrupt is recorded as interrupted

Cutting a response off mid-sentence — by saying the wake word over it — was
still recorded in the activity statistics as a completed answer.

2.21.0-ea.1 fixed this for an interruption during **thinking**, before the Echo
had started speaking. It missed the case it was actually written for: an
interruption during **playback**, once a response had begun. The two travel
different paths internally and only one was covered.

Measured here today: a response cut off mid-sentence, recorded as answered.

Both now record as **barged**. If you were reading the Activity tab across
earlier builds, turns you interrupted while the Echo was speaking were counted
as successful, and the totals will change once this is running — the fleet did
not change, the counting did.

## 2.21.0-ea.2 (Early Access)

Two fixes on top of 2.21.0-ea.1.

### Deploying a locally built firmware works again

Uploading your own compiled binary from the Updates tab failed with "an
internal error occurred". The controller was capping uploads at 1 MB against
a firmware roughly ten times that, so the file never reached the code meant
to handle it.

This broke on 2026-08-18 in 2.20.2, when a routine dependency update changed
how the web framework applies its size limit. Nothing else was affected —
updating from a published release goes by a different path and has worked
throughout. Only a locally built binary was blocked.

The limit is now 50 MB, and a file over it says so, with its size, instead of
reporting an internal error.

### Automatic update checks say when they are off

Setting `update_check_interval` to `0` stops the controller contacting GitHub
in the background. The Updates tab showed "No release info", which looks
exactly like GitHub being unreachable — so the tab you would open to find out
what was wrong could not tell you.

It now reads **Auto-checks off** beside the release line. **Check now** still
works and is unaffected: pressing it is a deliberate request, not background
traffic, and the result stays on screen until you press it again.

## 2.21.0-ea.1 (Early Access)

An Early Access build on top of 2.20.2. It supersedes 2.20.3-ea.1 and includes
everything in it — those notes are below.

Seven fixes, almost all of them things that failed quietly rather than
visibly. No database migration. One change needs new firmware and says so.

### A single bad message no longer drops the device

One malformed or unexpected control message could take a device's whole
connection down with it — the voice satellite, the Bluetooth proxy and the
audio channel together — and it reconnected every time it happened. The
message that triggered it in practice was a **playback statistics report**,
which is pure telemetry: the least important thing the device sends was able
to disconnect it.

Each message is now handled on its own. One that fails is logged with what it
was, and everything else carries on.

### Turning update checks off actually turns them off

Setting **Update check interval** to `0` was the obvious way to stop the
controller contacting GitHub, and it did the opposite: it removed the wait
between checks entirely, so the controller polled continuously until GitHub
rate-limited it. `0` now means off. A value that is not a number falls back to
the hourly default instead of silently killing update checking altogether.

### A dead music stream ends instead of hanging forever

If a Music Assistant stream stopped producing audio — an upstream failure
rather than the end of a track — playback waited indefinitely. Home Assistant
kept showing **playing** against silence, and nothing in the log said
otherwise.

A source that produces nothing for 30 seconds now ends the stream, tells Home
Assistant it is idle, and logs what happened. A stream that is merely slow
still recovers; the clock resets on every chunk that arrives.

### Media URLs are checked over HTTPS

Audio fetched over `https` did not verify the server's certificate. It does
now. If you stream from a server with a private or self-signed certificate,
point `EM_EXTRA_CA_CERT` at your CA and it will work as before. Playback
failures also now include the decoder's own error message, which previously
went nowhere.

### Interrupted answers are recorded as interrupted

A response cut off partway — by the button, or by the Echo hearing a wake word
mid-sentence — was recorded in the activity statistics as a completed answer.
A fleet interrupting a third of its responses read as a fleet answering
everything, which is how a real fault stayed hidden for two days. Those turns
now show as **barged**.

### The limiter's clipping counter reads correctly when it is off

Clipping caught while the limiter is switched off is the backstop doing its
job on a boosted EQ, not a fault. It was counted alongside genuine faults, so
a working setup looked broken. The two are now counted separately.

### The listening ring lights immediately — needs firmware v2.13.0

On an Echo doing its own wake word detection, the ring waited for a round trip
to the controller before lighting: measured at half a second, and longer on a
busy network. It now lights the moment the device hears you.

This one needs device firmware **v2.13.0** or later. On earlier firmware
everything behaves exactly as it does today, and the dashboard will offer the
update when it is published.

### Also

The command-line tools in `controller/tools/` now find the database under the
Home Assistant add-on as well as the standalone container.

## 2.20.3-ea.1 (Early Access)

An Early Access build on top of 2.20.2. The 2.20.2 notes below are the release
this builds on — you already have them if you were running an earlier build.

Two changes, both about the Echo hearing you. No database migration, no
firmware requirement, and nothing to do on your devices.

### Noise suppression no longer swallows quiet words

**Noise suppression** could cut speech to complete silence rather than just
turning it down. Measured on real turns: with it on, 8–15% of samples at a
healthy speaking level were digital silence, against 0.3% with it off. That is
the difference between a word sounding muffled and a word not being there at
all, and it lands hardest on exactly the quiet speech the setting is meant to
rescue.

Suppression is now limited to 20 dB. A passage the denoiser judges to be noise
is pushed well down instead of removed, and where it judges speech to be clean
it passes through completely untouched — so nothing that was working gets
quieter. Speech recognition stops improving well before 20 dB of noise
reduction, so the limit costs nothing that was being collected.

If you turned **Noise suppression** off because transcripts came back with
words missing, it is worth another try.

### Every Echo now knows all four wake words

Your Dot was given the recogniser for whichever wake word was selected when it
was provisioned. Choose a different one later and it needed that recogniser
copied across first — which normally happened, but left a gap: a device that
was **offline** when you changed the wake word was told to listen for a word it
did not have.

On a device doing its own wake word detection, that is a Dot that hears
nothing. Nothing warned, and the dashboard showed it as healthy.

All four stock wake words (Hey Jarvis, Alexa, Hey Mycroft, Hey Rhasspy) are now
installed on every device — 3 MB in total — so switching between them is
instant and cannot fail. Devices you provisioned earlier collect the missing
ones automatically the next time they connect, and any device found without the
recogniser it was told to use falls back to the controller listening on its
behalf, rather than going quietly deaf.

Custom wake words you have trained yourself are untouched and are still copied
across on selection.

## 2.20.2

**Better sound, and a long run of fixes.** Your Echo gets a speaker protection
stage that clears up the midrange and stops the equaliser distorting what it
boosts. Long answers no longer cut themselves off, wake words no longer fire at
nobody, interrupting a response no longer disconnects the device, Home
Assistant sidebar sign-in works again, and adding a second Echo no longer
overwrites the first.

**Two things to know before updating.** Your speaker will sound different —
the new protection stage is on by default. And the database migrates on first
start (a backup is taken automatically); for a small number of people that also
gives a device a new identity in Home Assistant, which is the last item here.
No firmware update is required and there is nothing to do on your devices.

### Your Echo sounds different, and it should sound better

Turning any equaliser band up used to push the audio past full scale, where it
was hard-clipped — measured at 4.7% of samples on a normal signal with a modest
bass boost, and 18% at the top of the sliders. A flat equaliser was unaffected,
so this only ever hit people who reached for the controls to improve their
sound.

There is now a limiter on the output, and a dynamic bass guard in front of it
that drops the low frequencies this driver physically cannot produce. The
second sounds backwards and is the bigger of the two: frequencies below about
115 Hz still move the cone even though you cannot hear them, and that movement
muddies everything above it — which is what "tin-can" actually is. Removing
them makes the midrange clearer and measurably *louder*, because the limiter no
longer has to hold the whole signal down to contain bass peaks nobody was going
to hear.

Both are on by default, as a single **Speaker protection** toggle under
Advanced → Playback. Leave it on. It was five separate controls during Early
Access and is now one, because none of the five could be judged by ear: the two
stages cancel out each other's most obvious effect, and the depth slider moves
the overall level by 0.14 dB across its entire range. Nothing was lost —
every setting still exists and still applies, and any value you had set is
still in force; the controls to change them again have gone from the dashboard.

Speaker settings also now take effect the moment you save them, mid-track,
rather than at the next track or the next restart.

### Long answers no longer cut themselves off

Ask for something lengthy — a story, a detailed explanation — and EchoMuse
would stop partway, sit with the ring lit, and end without finishing. It was
hearing its own voice as a wake word and cancelling itself. Measured on a real
device, a short reply peaked at 0.03 against the bar while a story reached
0.18 against a bar of 0.05.

Interrupting now needs two consecutive frames, and the default bar has moved
from 0.05 to 0.25. Talking over a response still works — real speech scores far
higher than the response does. **If you raised this setting yourself to stop
responses cutting out, you can put it back to the default.**

### Wake words no longer fire at nobody

The wake-word engine fills its buffer with random noise whenever it is reset,
and we scored that noise as if it were sound for the next 1.28 seconds. On one
device in one day that produced 19 wake events with nobody speaking, some
scoring higher than real speech. Raising your threshold would not have helped —
two of them cleared 0.80. Found, measured and fixed by @dmndru.

One consequence: interrupting a response is ignored for its first 1.28 seconds.

### Interrupting a response no longer disconnects the Echo

Saying the wake word mid-answer dropped the device off Home Assistant and
reconnected it a few seconds later — so the media player and voice assistant
flicked to unavailable each time. The cause was two lines of bookkeeping
sitting in an unreachable spot in the code, which only mattered when playback
was interrupted rather than finishing normally.

Also here: if the wake-word listener ever fails, it now restarts itself and
writes a loud error first, rather than leaving a device that hears you, lights
its ring, and never answers. A stuck "turn in progress" flag recovers the same
way. **We do not yet know what triggers that**, so if you see a device go deaf,
the log now contains the answer and we would like it.

### Home Assistant sign-in and admin rights

Opening EchoMuse from the Home Assistant sidebar failed and fell back to the
setup-token page — the lookup matching your HA account to an EchoMuse one
crashed on every request. Reported and diagnosed by @lennart24, fixed by
@finik.

Separately, if you had created a local EchoMuse account before ever opening the
panel through Home Assistant — including by copying `/data` across when moving
from the container, which the migration guide tells you to do — every Home
Assistant user was made read-only, permanently, with no way back but editing
the database by hand. Fixed, and existing installs in that state recover.

### Home Assistant behind your own certificate authority

If Home Assistant is served over HTTPS with a certificate from your own
internal CA, EchoMuse could not fetch the spoken response: the Echo woke up and
every turn ended silently. There is a new **Private CA certificate** option —
put the certificate (PEM) in Home Assistant's `ssl` folder and set the option
to `/ssl/<filename>`. On the standalone container, mount it and set
`EM_EXTRA_CA_CERT`. A missing or malformed file now stops the add-on starting
and says why.

Worth trying first, because it needs no certificate at all: if Home Assistant
itself still listens on plain HTTP behind something that terminates TLS, set
its *internal URL* to `http://<its-address>:8123`.

### Adding a second Echo no longer overwrites the first

Home Assistant identifies each satellite by a MAC address we derive from the
Echo's serial number. That derivation stripped letters out of the serial, so
two Echoes from the same batch differing only in their trailing characters
collapsed onto the same address and Home Assistant concluded they were one
device. Found and diagnosed by @lennart24.

That identity is now assigned once and stored, so a fix reaches only the
devices that need it. **On upgrade, every device keeps the identity it has
today unless it is in a colliding pair** — in which case the older device keeps
it and the other takes a new one, appearing in Home Assistant as a new device.
That device's entity IDs change and any automation naming them needs
repointing; it is the device that was being overwritten, so it had no working
entities to lose.

Also in this release: EchoMuse now carries a LICENSE and a contributing guide.

## 2.20.2-ea.7 (Early Access)

One addition, for people running Home Assistant over HTTPS with their own
certificate authority.

### Private certificate authorities are now supported

If Home Assistant is served over HTTPS using a certificate from your own
internal CA, EchoMuse could not fetch the spoken response. The controller
started normally and the Echo woke up, but every turn ended silently — the
audio fetch failed certificate verification, and nothing on screen said so.

There is a new **Private CA certificate** option. Put your CA certificate (PEM
format) in Home Assistant's `ssl` folder and set the option to
`/ssl/<filename>`. On the standalone container, mount the certificate and set
`EM_EXTRA_CA_CERT` to its path inside the container.

If the file is missing, unreadable, or not in PEM format, the add-on now
**fails to start and says why**, rather than starting and failing on every
voice turn afterwards with an error nothing connects back to the setting.

**Worth trying first, because it needs no certificate at all:** if Home
Assistant itself still listens on plain HTTP and something in front of it
handles TLS, setting Home Assistant's *internal URL* to
`http://<its-address>:8123` avoids the problem entirely — EchoMuse is on your
network, and the audio fetch is a local hop.

Nothing else changed, and nobody who is not using a private CA is affected. No
database migration, no firmware requirement, nothing to do on your devices.

## 2.20.2-ea.6 (Early Access)

One fix: interrupting a long answer dropped the device off Home Assistant.

### Barging in during a response disconnected the Echo

Say the wake word while EchoMuse is answering and the device would drop its
connection and reconnect a few seconds later. Everything came back on its own,
but Home Assistant lost the satellite briefly each time — so the media player
and the voice assistant flicked to unavailable, and anything mid-flight was
lost.

The cause was two lines of internal bookkeeping that had been sitting in an
unreachable spot in the code for months, so a value the controller expected
after playback was never set up. It only mattered when playback was
*interrupted* rather than finishing normally, which is why it surfaced now that
interrupting works properly.

Interrupting a response is now what it should be: the answer stops, your new
request is heard, and the device stays connected throughout.

Also in this release, a guard against the same class of mistake anywhere in the
controller, and a limit on how fast the wake-word listener may restart itself
if it ever fails repeatedly — that recovery was added in 2.20.2-ea.5 and could
have monopolised the controller in the worst case.

Nothing required of you: no database migration, no firmware requirement, and
nothing to do on your devices.

## 2.20.2-ea.5 (Early Access)

One fix, and it is for the fault that showed up while testing 2.20.2-ea.4.

### A device could stop responding to the wake word until the add-on restarted

It went quiet with nothing in the log to say so, and — the confusing part —
the Echo itself carried on detecting the wake word perfectly. On devices doing
their own wake word detection, the Echo hears you, decides it heard you, and
tells the controller; the part of the controller that acts on that had stopped
running. So the device looked healthy from every angle and simply never
answered.

Two things could leave it in that state and neither said anything. The loop
that listens for wake words could end on an unexpected error and nothing
restarted it or noticed. Separately, the flag that says "a voice turn is in
progress" could be left on after something went wrong mid-turn, and while it
is on the device deliberately ignores the microphone.

Both now recover on their own, and both write a loud error to the log first,
so a recurrence explains itself instead of looking like the device has gone
deaf for no reason. **If you saw this on 2.20.2-ea.4, please still send the
log if you have it** — the fix makes it survivable, but we do not yet know
what triggered it.

Nothing else changed. No database migration, no firmware requirement, nothing
to do on your devices.

## 2.20.2-ea.4 (Early Access)

Four fixes. Two change what you hear, one unblocks Home Assistant users who
could not administer the add-on, and one is a change to the dashboard's
speaker controls.

### Long answers no longer interrupt themselves

Ask for something lengthy — a story, a detailed explanation — and EchoMuse
would stop partway, sit with the ring lit, and then end without finishing.
It was hearing its own voice as a wake word and cancelling itself.

The detector that listens for you interrupting a response used to act on a
single 80ms fragment, at a deliberately low confidence bar. That was fine for
short replies, which never got near it, and wrong for long ones: continuous
speech offers far more chances to sound briefly like a wake word. Measured on
a real device, a short reply peaked at 0.03 while a story reached 0.18 against
a 0.05 bar.

Two frames in a row are now required, and the default bar has moved from 0.05
to 0.25. Interrupting still works — talking over a response scores far higher
than the response itself does. **If you had raised this setting yourself to
stop responses cutting out, you can put it back to the default.**

### The speaker settings are now one switch

The bass guard and limiter were five controls. None of them could be judged by
ear: the two stages cancel out each other's most obvious effect, and the depth
slider moves the overall level by 0.14dB across its entire range. They are now
a single **Speaker protection** toggle under Advanced, in the Playback section,
and it should be left on.

Nothing is lost — every setting still exists and still applies, and anything
you had set is still in force. If you tuned the limiter ceiling or the guard
depth, those values are unchanged; the controls to change them again have gone
from the dashboard.

The bass guard's default depth also moved from −20dB to −30dB. You will not
hear the difference, and it is not meant to be heard: it puts the default in
the middle of the range so there is room to adjust either way.

### Home Assistant users could be locked out of administering the add-on

If you set up a local EchoMuse account before opening the panel through Home
Assistant — including by copying your existing `/data` across when moving from
the container, which the migration guide tells you to do — every Home
Assistant user was created read-only, permanently. The only way back was
editing the database by hand.

The rule was counting accounts rather than accounts that can actually sign in
through Home Assistant, and a local password account cannot: the panel signs
you in through Home Assistant before it ever offers a login form. Fixed, and
the dashboard now takes your role from the server on load rather than
remembering it, so a role that is corrected or promoted takes effect on the
next page load instead of the next sign-in.

**If you are currently stuck read-only, updating should be enough.** No
database editing and no cache clearing.

### Speaker processing now says what it is doing

The add-on log records what the output chain is set to, whenever a stream
starts and whenever you change a setting, and how much work each stage
actually did. This is diagnostic only and changes nothing about playback — it
exists because "is this setting doing anything?" was not answerable from
outside, which cost four separate listening tests.

## 2.20.2-ea.3 (Early Access)

One fix, and it is the one that makes the previous release's headline feature
actually work.

### Speaker settings now take effect when you save them

Saving a limiter or bass guard setting updated the stored configuration but
never reached the running controller — those settings are applied by the
controller rather than by the Echo, and only a device reconnect picked them
up. So they appeared to do nothing, and would then quietly start working after
a restart, which is the point at which most people would stop investigating.

If you tried tuning the speaker and heard no difference, that was correct: the
settings were not reaching the audio. Together with the live updating added in
2.20.2-ea.2, moving the bass guard depth or the limiter ceiling is now audible
immediately, on the track you are already playing.

Worth re-doing any tuning you did before this release — you were listening to
unchanged sound.

## 2.20.2-ea.2 (Early Access)

Two changes, both about being able to tell what your system is doing. No new
database migration, no firmware requirement.

### Speaker settings now change while the music is playing

The limiter and bass guard settings only took effect when the next track
started, so changing one while listening appeared to do nothing at all. That
made them very hard to judge: by the time a new track began, the sound you
were comparing against was gone.

They now apply immediately, mid-track. Turning the bass guard on or off, or
moving its depth, is audible straight away and does not click or interrupt
playback. The equaliser behaves the same way.

If you have been trying to tune the speaker and concluded the settings made no
difference, this is why — it is worth another listen.

### Early Access uses its own ports

The Early Access and stable add-ons keep completely separate data, including
their own list of devices — but both handed out the same port numbers to the
Home Assistant satellites they create. Switching between them could therefore
leave Home Assistant talking to the wrong device, which showed up as devices
sitting unavailable, wake words lighting the ring, and every request ending
immediately with no answer.

Early Access now uses a separate range, so a leftover Home Assistant entry
from the other channel simply shows as unavailable instead of reaching
something unexpected. Existing devices keep the ports they already have;
nothing is renumbered.

**Switching channels is still a migration.** Each add-on has its own database
and its own certificate authority, so your devices will not connect to the
other channel until you copy `/data` across — see the Documentation tab.

## 2.20.2-ea.1 (Early Access)

Two fixes that stop the controller doing something wrong, and the first two
steps of making the speaker sound better. No new database migration, no
firmware requirement.

### Home Assistant sign-in through the sidebar works again

Opening EchoMuse from the Home Assistant sidebar failed and fell back to the
setup-token page. The lookup that matches your Home Assistant account to an
EchoMuse one crashed on every request, so automatic sign-in — the main reason
to run the add-on rather than the standalone container — has been unusable for
several releases. Reported and diagnosed by @lennart24, fixed by @finik.

### Wake words no longer fire at nobody

The wake-word engine fills its buffer with random noise whenever it is reset,
and we scored that noise as if it were sound for the next 1.28 seconds. On one
device in one day that produced **19 wake events with nobody speaking**, all of
them just under a second after a turn ended, some scoring higher than real
speech does. Worse, it could fire the barge-in detector and cancel an answer
you were waiting for.

Raising the wake-word threshold would not have helped — these score higher than
real speech, and two of the recorded events cleared 0.80. Found, measured and
fixed by @dmndru.

One consequence worth knowing: interrupting a response is now ignored for the
first 1.28 seconds of it. That is the right trade against turns being cancelled
by nobody.

### The equalizer no longer distorts what it boosts

Turning any EQ band up could push the audio past full scale, and it was being
hard-clipped — measured at **4.7% of samples** on a normal signal with a modest
bass boost, and 18% at the top of the sliders. Flat EQ was unaffected, so this
only ever hit people who reached for the controls to improve their sound.

There is now a limiter on the output. Same test signal, same settings, no
clipping at all, for 1–6 dB of gain reduction rather than the 12 dB a simple
volume trim would have cost. **Limiter** and its ceiling and release are in
Playback; leave it on.

### New: Bass guard

A dynamic control that drops bass the speaker physically cannot produce.

It sounds backwards, and it is the single biggest thing available for a driver
this size. Frequencies below about 115 Hz still move the cone even though you
cannot hear them, and that movement muddies everything above — which is what
"tin-can" actually is. Removing them makes the midrange clearer, and measurably
*louder*, because the limiter no longer has to hold the whole signal down to
contain bass peaks nobody was going to hear. Quiet passages keep their low end;
only loud content is affected.

The parameters come from measurements of the stock Amazon firmware on the same
speaker. **Bass guard depth** is in Playback, defaulting to −20 dB where stock
uses −40 dB — deliberately gentler, because stock pairs its setting with an
equalizer curve we have not measured.

**This one wants your ears.** Play something with real low end and try the depth
from 0 to −40. Expect it to sound thinner at first and then clearer; the best
setting is probably deeper than feels right.

### Also

- Every device now carries all four stock wake word models, so changing wake
  word no longer requires a device to have been provisioned with it.
- Security updates to the web server, TLS and mDNS libraries.
- The rooting guide now says the unlock needs Linux, not macOS. Thanks
  @StefanOltmann.

## 2.20.1

Everything from the 2.20.1 Early Access builds. All fixes — no new settings,
no database migration, no firmware requirement, and nothing to do on your
devices.

### Interrupting a response now works

Saying the wake word while EchoMuse was answering stopped it — and then
nothing happened. Whatever you said next was never heard, so you had to wait
and ask again. The interrupting turn was ending a few milliseconds after it
began, before any audio reached it.

Home Assistant runs one voice pipeline at a time for each device, and EchoMuse
was starting a second one without telling it to stop the first; the abandoned
pipeline's ending then arrived and closed the new turn. Fixed for both cases —
interrupting while it is thinking, and while it is speaking.

Interrupting is still a one-way door: say the wake word and then stay quiet,
and the original answer is gone rather than resumed.

### Announcements

- **They now finish before Home Assistant is told they have.**
  `assist_satellite.announce` returned the moment the request arrived rather
  than when the audio stopped, so an automation playing two announcements in a
  row could have them talk over each other, and the satellite dropped out of
  its "responding" state early. An announcement now also reports whether the
  audio actually reached the speaker.
- **A cancelled voice turn no longer silences every announcement after it.**
  Press the action button to stop a turn, or mute the device, and
  announcements stopped playing there — no error, nothing in the dashboard,
  and it stayed that way until a voice turn happened to run. With more than
  one device this looked like announcements moving to the wrong device.
  Long-standing, and only found by trying enough announcements in a row.

### Wake words

- **Changing a wake word installs it before switching the device onto it.**
  If you picked a wake word a device had never been given, and that device was
  doing its own wake word detection, it stopped answering entirely — it had no
  model to listen with, and nothing said so while the dashboard showed the
  device as healthy. The device now keeps listening for its current wake word
  until the new model has arrived, then switches. If the model cannot be
  installed it stays where it is and the device log says why.

  Devices using controller-side detection are unaffected — the model file
  means nothing to them, so the change stays immediate.

  Note that changing a wake word briefly reconnects the device in Home
  Assistant, which makes every entity for it flicker unavailable and back. If
  you have an automation with a **state trigger** on one of them it will fire.
  See the configuration guide under *Wake word model*.

### Elsewhere

- **The dashboard shows Speaking again.** A device tile sat on "Thinking" for
  the whole spoken response. It now follows the device's own report that the
  audio has stopped, rather than the moment the controller finished sending it
  — which happens almost instantly and left several seconds of audio still to
  play. The start of a response is still estimated and can lead the speaker by
  about a second.
- **Firmware is downloaded once per release, not once per device.** Updating
  several devices, or setting up several through the provisioning wizard,
  re-downloaded the same ~10MB every time. It is now kept beside your database
  and checked against its fingerprint before use.
- **A guide for moving from the Docker container to the Home Assistant
  add-on**, in `docs/migrate-to-addon.md`. Read the part about `tls/` before
  you start.

## 2.20.1-ea.5 — Early Access

- **Changing a wake word now installs it before switching the device onto
  it.** In ea.3 and ea.4, picking a wake word a device did not have made the
  controller take over the listening for that device while it installed the
  model — even if you had deliberately chosen on-device detection. It worked,
  but it quietly changed a setting you had chosen and the dashboard carried on
  showing the setting you asked for.

  The device now keeps listening for its current wake word, on the device,
  until the new model has actually arrived — then it switches. If the model
  cannot be installed, the device stays on the wake word it already has and
  the device log says so. Nothing you chose gets overridden.

  Devices that use controller-side wake word detection are unaffected: the
  model file means nothing to them, so the change applies immediately as
  before.

## 2.20.1-ea.4 — Early Access

- **The dashboard's Speaking state now follows the device.** In ea.3 the tile
  showed Speaking slightly early, dropped back to Thinking while the device
  was still talking, then went idle. It was following when the controller
  finished *sending* audio, which happens almost instantly — the device still
  had several seconds of it left to play. It now waits for the device to
  report that the audio has actually stopped.

  The start of a response is still estimated and can lead the speaker by about
  a second: the device deliberately holds audio until it has enough buffered,
  and it only tells the controller when playback *ends*. Reporting the start
  too needs a firmware change.

## 2.20.1-ea.3 — Early Access

- **Changing a device's wake word can no longer leave it deaf.** If you picked
  a wake word a device had never been given, and that device was doing its own
  wake word detection, it stopped answering entirely — it had no model to
  listen with, and the controller had already stepped back from listening on
  its behalf. Nothing said so, and the dashboard showed the device as healthy.
  The controller now keeps listening for that device while it installs the
  model, and stays listening if the install fails.

- **The dashboard shows Speaking again.** A device tile sat on "Thinking" for
  the whole spoken response. The tile was only ever told about listening,
  thinking and the end of the turn, so it never heard about the bit in
  between.

- **Firmware is downloaded once per release, not once per device.** Updating
  several devices, or setting up several through the wizard, re-downloaded the
  same ~10MB every time. It is now kept on disk beside your database and
  checked against its fingerprint before use.

## 2.20.1-ea.2 — Early Access

Two announcement faults found while testing ea.1.

- **A cancelled voice turn used to silence every announcement after it.**
  Press the action button to stop a turn — or mute the device — and
  announcements stopped playing on that device, with no error and nothing in
  the dashboard to show for it. It stayed that way until a voice turn happened
  to run. Long-standing; ea.1's announcement changes are simply what made
  anyone try enough announcements in a row to find it. If you have more than
  one device, this looked like announcements moving to the wrong device: the
  others were fine, because only the cancelled one was affected.

- **`media_player.play_media` with announce turned on failed outright in
  ea.1.** A rename in that release missed this second announcement path, so
  every call raised an internal error and nothing played.
  `assist_satellite.announce` was unaffected.

An announcement now also tells Home Assistant whether the audio actually
reached the speaker, rather than always reporting success.

## 2.20.1-ea.1 — Early Access

- **Talking over a response now works.** Saying the wake word while EchoMuse
  was answering did stop it — and then nothing happened. Whatever you said
  next was never heard: the interrupting turn ended a few milliseconds after
  it began, before any audio was captured, so you had to wait and start again.
  Home Assistant runs one voice pipeline at a time per device, and EchoMuse
  was starting a second without telling it to stop the first; the old
  pipeline's ending then arrived and closed the new turn. Fixed for both
  cases — interrupting while it is thinking, and while it is speaking.

  Note that interrupting is still a one-way door. If you say the wake word and
  then stay quiet, the original answer is gone rather than resumed.

- **Announcements now finish before Home Assistant is told they have.** The
  `assist_satellite.announce` service returned the moment the request
  arrived rather than when the audio stopped. An automation that plays two
  announcements in a row could have them talk over each other on the device,
  and the satellite dropped out of its "responding" state early. It now waits
  for playback, and says whether the audio actually reached the speaker.

## 2.20.0-ea.6 — Early Access

An Early Access build on top of 2.20.0. The 2.20.0 notes below are the
release this builds on — you already have them if you were running an
earlier Early Access build.

- **Installing wake word models on a device works again.** Sending a wake
  word model to an Echo failed with an internal error, every time. If you
  changed a device's wake word to one it had not been given during setup, it
  simply stopped responding — it had no model to listen with, and nothing
  said so. Fixed, with a test so it cannot come back quietly.

## 2.20.0

Everything since 2.19.0. Two changes need a moment of your attention — the
wake word name and the volume range — and both are called out below.

### Adding a device in Home Assistant now works

Adding an EchoMuse device opened a dialog asking you to say the wake word,
and it never advanced: the ring lit, the device answered, and **Skip** was
the only way on. Home Assistant listens for the *name* of the wake word that
fired and the controller never sent one. Every ordinary voice turn already
worked, which is why this went unnoticed for so long — the setup dialog is
the only thing that reads it.

Two follow-on fixes came out of the same flow. The device now stops
listening the moment Home Assistant is finished, instead of holding the
microphone until a timeout — which is what made the dialog's second prompt
land on a device still busy with the first, and what caused the occasional
"device not found, press Retry". And the ring holds briefly so you can see
it heard you, rather than flashing for a tenth of a second.

### ⚠️ Your wake word may need selecting again

The wake word reported to Home Assistant loses its version suffix: **"hey
jarvis v0.1" becomes "hey jarvis"**. The `_v0.1` is an openWakeWord filename
convention that was never meant to be read by a person, and it had been
reaching Home Assistant's own device registry.

Home Assistant restores a wake word choice **by name**, so after updating, a
device may show none selected. Set it again under Settings → Devices &
Services. One dropdown, once per device.

### ⚠️ Volume distorted above about three-quarters, and no longer does

EchoMuse was driving the codec's digital volume past the point where it can
only clip — measured at **65% distortion three button presses above the
midpoint, and 89% at the maximum**, with the output no longer getting any
louder. Stock Alexa never touches that control. This is the substance of
every report that EchoMuse sounded worse than stock when turned up. Found
and measured by @kdkavanagh.

Two things you will notice:

- **The volume percentage in Home Assistant will read higher** for a device
  sitting at the same physical level, because the scale no longer includes a
  stretch that only distorted. Nothing got louder or quieter. **If you have
  an automation with a volume threshold in it, check that threshold.**
- **The top of the range is quieter than it was.** What used to be above it
  was a square wave, so this is the fix rather than a regression — but if
  you regularly ran a device near maximum you will hear it. Cleaner, and not
  as loud.

The physical buttons now step about 4dB per press across the audible range,
instead of spending presses near the bottom of a scale where nothing is
audible — silencing a device is the mute button's job. The volume ring spans
that same range, so a press always moves it.

### Home Assistant add-on

- **Leaving "Server IP" empty now detects this host's LAN address.** The old
  fallback was a hardcoded address, so a fresh install with the field blank
  advertised it to every device over mDNS: the controller ran perfectly and
  no device could reach it, with nothing reporting an error anywhere. If you
  set the field by hand to work around that, you can clear it.
- **No separate dashboard login under Home Assistant.** HA has already
  authenticated you, so the panel signs you in as your HA user and the
  first-run setup token is gone. The first person through becomes admin;
  everyone after is read-only until promoted. There is no Sign out on an HA
  session — sign out of Home Assistant instead.
- **Recordings and transcripts are admin-only.** Saved utterances are
  recognisable speech from inside your home, and every household HA user can
  reach the panel, so read-only accounts no longer see the audio player or
  the transcript text. Enforced on the server, not hidden in the page.
- **A Debug logging option.** The controller could always do this, but only
  if you ran it as a container and knew the environment variable. Leave it
  off unless you are chasing something; it is verbose.
- **An Early Access channel**, installed as a separate add-on for anyone who
  wants the next release before it is general. It has its own storage, so
  switching channels is a migration rather than a toggle — see its
  documentation before installing.
- The provisioning wizard's WebUSB error now names the exact origin the
  browser needs allowed, instead of suggesting an address the add-on refuses.

### Elsewhere

- **Entity names no longer repeat the device name.** Home Assistant already
  puts the device name in front of every entity and we were adding it again,
  so a sensor read "Kitchen Voice Assistant Kitchen Ambient Light". Only the
  displayed name changes: entity IDs stay as they are, so automations keep
  working, and an entity you renamed yourself keeps your name.
- **Greyed-out switches no longer do the opposite of what they show.** A
  disabled toggle could still be clicked, and stored the inverted value.
  Thanks to @kdkavanagh.
- Roles can be changed via `PATCH /api/users/{id}`; the last admin cannot be
  demoted.
- A voice turn that Home Assistant ends before it starts listening is no
  longer recorded as "no speech" — it was blaming the speaker for something
  at the other end, in a figure the activity statistics report.
### Early Access prereleases

This release was published to the Early Access channel first, as
`2.20.0-ea.1` through `2.20.0-ea.5`. Each carried a subset of the above;
`2.20.0-ea.5` is the same set of changes as this release. Nothing further is
needed if you were running one of them.


## 2.19.0

- **EchoMuse can be installed as a Home Assistant add-on** rather than run
  with docker compose by hand. The dashboard appears as a sidebar panel
  through ingress, so it is reachable wherever Home Assistant is without
  exposing another port. Thanks to @natecj for building it, and @Pinball3D
  whose earlier attempt worked out the approach.
- Existing docker compose installs are unaffected.
- **Six shipped defaults corrected — new installs only.** Wake threshold
  0.3 → 0.5, beamforming, echo cancellation and barge-in on by default,
  barge-in threshold 0.10 → 0.05. Stored configuration always wins over a
  shipped default, so an existing controller keeps every value it has.

## 1.0.1

- Initial Home Assistant Supervisor add-on packaging: install and run the
  controller from Settings → Add-ons instead of hand-run docker-compose.
- Ingress support for the dashboard (no separate port to expose).
- Add-on config UI labels, icon, and logo.
