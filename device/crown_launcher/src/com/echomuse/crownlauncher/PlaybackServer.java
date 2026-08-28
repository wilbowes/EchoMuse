package com.echomuse.crownlauncher;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Process;
import android.util.Log;

import java.io.IOException;
import java.io.InputStream;

/**
 * Crown-only mitigation for the DL1 playback freeze — see
 * docs/echo-show-8-audiotrack-design.md and
 * docs/echo-show-8-freeze-scenarios.md (Scenario C). The Go daemon no
 * longer opens /dev/snd directly for playback on this board: it feeds raw
 * PCM to this socket, and AudioTrack lets mediaserver arbitrate the
 * hardware normally instead of the daemon holding it open exclusively —
 * that exclusive hold is what the pstore evidence in the freeze doc showed
 * colliding with mediaserver's own HAL access.
 *
 * Format is fixed at build time, matching pcm_speaker_crown.go's ALSA
 * config exactly (48kHz stereo S16LE) — no runtime handshake, since both
 * sides ship from the same repo. Wire framing: 4-byte little-endian
 * length prefix, then that many raw PCM bytes, repeated per period.
 *
 * Uses Android's ABSTRACT socket namespace (LocalServerSocket's only real
 * option — its String constructor always binds abstract; a first attempt
 * here tried to force FILESYSTEM via LocalSocketAddress.getName() and
 * silently got abstract anyway, confirmed live via
 * /proc/&lt;pid&gt;/net/unix showing an "@"-prefixed entry, not a real
 * dentry — ls/find on the "path" found nothing because there never was
 * one). Simpler than fighting it: abstract sockets need no stale-file
 * cleanup and aren't gated by filesystem permission bits at all, which
 * only strengthens the Q1 answer in the design doc (works from both the
 * app-uid and root-exec launch paths). The Go side dials the matching name
 * with Go's "@name" convention for Linux abstract sockets.
 *
 * One connection at a time (the daemon only ever runs once); a new
 * connection replaces whatever the old one was doing. On any socket error
 * the AudioTrack is released and rebuilt fresh on the next connection —
 * deliberately no retry/health-check machinery beyond that, same "minimal,
 * no supervisor" reasoning as ServerService itself.
 *
 * Cross-app ducking rides transient AudioFocus, not our own mixer: once
 * this became a real AudioTrack client of AudioFlinger (rather than an
 * exclusive /dev/snd hold), other apps' streams are outside our mixer's
 * reach entirely — requesting focus per turn is what makes them duck/pause
 * automatically instead. See "What's NOT done" #1 in
 * docs/echo-show-8-audiotrack-handoff.md.
 */
class PlaybackServer implements Runnable {
    private static final String TAG = "EchoMusePlayback";

    // Must match socket_pcm_crown.go's crownPlaybackSocket minus the "@"
    // (Go's abstract-namespace convention prefixes it; Android's name is
    // the same string, prefix-free).
    static final String SOCKET_NAME = "com.echomuse.crownlauncher/pcm";

    private static final int SAMPLE_RATE = 48000;
    private static final int CHANNEL_MASK = AudioFormat.CHANNEL_OUT_STEREO;
    private static final int ENCODING = AudioFormat.ENCODING_PCM_16BIT;

    private final AudioManager audioManager;

    private volatile boolean running = true;
    private LocalServerSocket serverSocket;
    // Tracked so stop() can close it directly — see StatusOverlay's
    // identical field for why (closing only serverSocket doesn't wake a
    // thread parked reading mid-connection, found in review 2026-08-27).
    private volatile LocalSocket currentClient;

    PlaybackServer(Context context) {
        audioManager = context.getSystemService(AudioManager.class);
    }

    void stop() {
        running = false;
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (IOException ignored) {
        }
        try {
            if (currentClient != null) currentClient.close();
        } catch (IOException ignored) {
        }
    }

    @Override
    public void run() {
        try {
            serverSocket = new LocalServerSocket(SOCKET_NAME);
        } catch (IOException e) {
            Log.e(TAG, "could not bind " + SOCKET_NAME + ": " + e);
            return;
        }
        Log.i(TAG, "listening on abstract socket \"" + SOCKET_NAME + "\"");

        while (running) {
            LocalSocket client;
            try {
                client = serverSocket.accept();
            } catch (IOException e) {
                if (running) Log.w(TAG, "accept failed: " + e);
                continue;
            }
            // Abstract-namespace sockets carry no filesystem permission
            // bits at all (the whole reason this socket uses one — see the
            // class comment), so anything on the device can connect here.
            // The daemon always runs as THIS app's own uid (exec'd child
            // inherits it), so that's the one peer this socket should ever
            // accept — found in review 2026-08-27: an unchecked accept
            // meant any other installed app could hijack playback (inject
            // arbitrary PCM, or just deny the real daemon's audio) with no
            // root needed.
            try {
                Credentials creds = client.getPeerCredentials();
                if (creds.getUid() != Process.myUid()) {
                    Log.w(TAG, "rejecting connection from uid " + creds.getUid()
                            + " (expected " + Process.myUid() + ")");
                    try { client.close(); } catch (IOException ignored) { }
                    continue;
                }
            } catch (IOException e) {
                Log.w(TAG, "could not read peer credentials, rejecting: " + e);
                try { client.close(); } catch (IOException ignored) { }
                continue;
            }
            Log.i(TAG, "daemon connected");
            currentClient = client;
            try {
                handleConnection(client);
            } catch (IOException e) {
                Log.w(TAG, "connection ended: " + e);
            } finally {
                currentClient = null;
                try {
                    client.close();
                } catch (IOException ignored) {
                }
            }
        }
    }

    private void handleConnection(LocalSocket client) throws IOException {
        AudioTrack track = buildTrack();
        track.play();
        AudioFocusRequest focusRequest = null;
        int silentStreak = 0;
        try {
            InputStream in = client.getInputStream();
            byte[] lenBuf = new byte[4];
            while (running) {
                readFully(in, lenBuf, 4);
                int len = (lenBuf[0] & 0xFF) | (lenBuf[1] & 0xFF) << 8
                        | (lenBuf[2] & 0xFF) << 16 | (lenBuf[3] & 0xFF) << 24;
                if (len <= 0 || len > 1 << 20) {
                    throw new IOException("bad frame length " + len);
                }
                byte[] frame = new byte[len];
                readFully(in, frame, len);
                track.write(frame, 0, len);

                // This connection lives for the daemon's whole run (its own
                // mix loop never closes the socket, and idle periods arrive
                // as real, continuously-written silence — see
                // pcm_speaker_crown.go's silenceLoop) — so focus must track
                // audio activity within the stream, not the connection.
                // Holding it for the connection's lifetime would duck other
                // apps permanently from boot instead of per turn, which is
                // what actually happened on the first cut of this: measured
                // live via `dumpsys audio`, one requestAudioFocus() at
                // connect and no abandon ever, 2026-08-26.
                if (isSilent(frame)) {
                    silentStreak++;
                    if (focusRequest != null && silentStreak >= SILENCE_DEBOUNCE_FRAMES) {
                        audioManager.abandonAudioFocusRequest(focusRequest);
                        focusRequest = null;
                    }
                } else {
                    silentStreak = 0;
                    if (focusRequest == null) focusRequest = requestDuckFocus();
                }
            }
        } finally {
            track.stop();
            track.release();
            if (focusRequest != null) audioManager.abandonAudioFocusRequest(focusRequest);
        }
    }

    // Debounced rather than dropping focus on the very first silent frame:
    // ordinary speech has brief internal gaps, and reacquiring focus on
    // every one would flap other apps' volume mid-sentence. ~6 frames is a
    // deliberately short window — one write is one ALSA period from
    // pcm_speaker_crown.go's mix loop (~32ms), so this is roughly 200ms of
    // continuous silence before releasing.
    private static final int SILENCE_DEBOUNCE_FRAMES = 6;

    private static boolean isSilent(byte[] frame) {
        for (byte b : frame) {
            if (b != 0) return false;
        }
        return true;
    }

    // AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK, not the exclusive TRANSIENT: a
    // wake response is short and other apps (browser audio, etc.) should
    // duck under it, not stop outright, matching the pre-fix behaviour
    // where our own mixer only ever ducked its own planes. Best-effort —
    // a denied request just means no ducking this turn, not a broken turn,
    // so the return value isn't checked beyond logging it.
    private AudioFocusRequest requestDuckFocus() {
        if (audioManager == null) return null;
        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build();
        AudioFocusRequest request = new AudioFocusRequest.Builder(
                AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                .setAudioAttributes(attrs)
                .build();
        int result = audioManager.requestAudioFocus(request);
        if (result != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
            Log.w(TAG, "audio focus request denied (" + result + "), no ducking this turn");
        }
        return request;
    }

    private static void readFully(InputStream in, byte[] buf, int len) throws IOException {
        int off = 0;
        while (off < len) {
            int n = in.read(buf, off, len - off);
            if (n < 0) throw new IOException("socket closed mid-frame");
            off += n;
        }
    }

    private AudioTrack buildTrack() {
        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build();
        AudioFormat format = new AudioFormat.Builder()
                .setSampleRate(SAMPLE_RATE)
                .setChannelMask(CHANNEL_MASK)
                .setEncoding(ENCODING)
                .build();
        // getMinBufferSize is a hard floor for MODE_STREAM at this config —
        // measured live at 18464 bytes (device/crown_launcher's
        // AudioProbeReceiver, 2026-08-26) regardless of what's requested
        // below it. PERFORMANCE_MODE_LOW_LATENCY was granted at that size in
        // the same probe run, so there's no latency win being left on the
        // table by not fighting this floor.
        int minBuf = AudioTrack.getMinBufferSize(SAMPLE_RATE, CHANNEL_MASK, ENCODING);
        return new AudioTrack.Builder()
                .setAudioAttributes(attrs)
                .setAudioFormat(format)
                .setBufferSizeInBytes(minBuf)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                .build();
    }
}
