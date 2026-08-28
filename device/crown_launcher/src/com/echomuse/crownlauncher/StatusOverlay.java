package com.echomuse.crownlauncher;

import android.animation.ValueAnimator;
import android.content.Context;
import android.graphics.BlurMaskFilter;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RectF;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.Process;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/**
 * crown's answer to biscuit's LED ring — a glowing bar along the TOP edge
 * of the screen, drawn over whatever app has focus (browser, home screen,
 * anything). device/internal/bindings/led/led_crown.go's overlayController
 * forwards every SetLEDs call here over a Unix socket, the same shape as
 * PlaybackServer for audio: the Go daemon cannot draw Android UI directly
 * any more than it can own an AudioTrack directly, so crown_launcher does
 * the one thing on each side of that line the daemon can't do itself.
 *
 * First cut (2026-08-26) put the bar at the BOTTOM — reads as a stray line
 * above the on-screen buttons rather than a status cue, since that's
 * exactly where the on-screen chrome already lives. A full-perimeter
 * border (traced around all four edges, the direct LedRing analogue) was
 * tried next and is more than this needs — top edge only, same treatment.
 *
 * TYPE_APPLICATION_OVERLAY, FLAG_NOT_TOUCHABLE|FLAG_NOT_FOCUSABLE — the bar
 * must never intercept a touch or steal focus from whatever the user is
 * actually looking at. Needs SYSTEM_ALERT_WINDOW, granted by
 * provision_crown.sh via `appops set` right after install (see
 * AndroidManifest.xml) — no manual Settings trip.
 *
 * One "LED": there is no ring here, so unlike PlaybackServer there is
 * nothing per-connection to build or tear down. The view is added once,
 * hidden, and toggled/recoloured in place for the life of the process.
 */
class StatusOverlay implements Runnable {
    private static final String TAG = "EchoMuseOverlay";

    // Must match device/internal/bindings/led/led_crown.go's
    // crownLedSocket minus the "@" — same abstract-namespace convention
    // PlaybackServer.SOCKET_NAME already uses.
    static final String SOCKET_NAME = "com.echomuse.crownlauncher/led";

    // Safety cap on the wake lock — a dead-man's switch, not a target
    // duration. Turn end (colour -> off) releases it well before this in
    // the ordinary case; this only bounds how long the screen stays lit if
    // that message is somehow lost, same reasoning as every TTL elsewhere
    // in this project (see ledScene ttlSec in CLAUDE.md's LED priority
    // section) — sized generously against the 135s spinner TTL so a long
    // HA think time never gets cut off mid-turn.
    private static final long MAX_TURN_WAKE_MS = 180_000;

    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private volatile boolean running = true;
    private LocalServerSocket serverSocket;
    // Tracked so stop() can close it directly. Found in review 2026-08-27:
    // closing only serverSocket unblocks a thread parked in accept(), but
    // one instead blocked inside handleConnection()'s in.readLine() (daemon
    // connected but between messages) was never woken — a stale thread and
    // its socket leaking past every stop()/restart cycle.
    private volatile LocalSocket currentClient;
    private WindowManager windowManager;
    private TopBarView topBarView;
    private PowerManager.WakeLock wakeLock;
    private boolean turnActive = false;

    StatusOverlay(Context context) {
        this.context = context;
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
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        mainHandler.post(new Runnable() {
            @Override public void run() { removeView(); }
        });
    }

    @Override
    public void run() {
        mainHandler.post(new Runnable() {
            @Override public void run() { addView(); }
        });

        PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
        // SCREEN_BRIGHT_WAKE_LOCK|ACQUIRE_CAUSES_WAKEUP turns the screen on
        // even from a locked/asleep state with no Activity involved (this
        // is a Service — there's nothing to setShowWhenLocked/
        // setTurnScreenOn on). ON_AFTER_RELEASE hands the screen back to
        // Android's ordinary timeout/sleep behaviour on release rather than
        // forcing it off immediately, so a turn ending doesn't visibly snap
        // the screen dark before the user has had a chance to read it.
        // Deprecated APIs (replaced on Activities by the flags above), but
        // still the only way to wake the screen from a bare Service.
        wakeLock = pm.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK
                        | PowerManager.ACQUIRE_CAUSES_WAKEUP
                        | PowerManager.ON_AFTER_RELEASE,
                "EchoMuse:StatusOverlay");

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
            // Same reasoning as PlaybackServer's identical check: an
            // abstract-namespace socket has no filesystem permission bits,
            // so anything on the device can connect. This one only lets a
            // connected peer toggle LED colour / hold a wake lock, lower
            // stakes than hijacking audio, but still no reason to accept it
            // from anything but the daemon (which always runs as this
            // app's own uid).
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

    // Newline-delimited JSON, not PlaybackServer's framed binary — this
    // carries a handful of messages per second at the busiest (a pulse
    // animation), not audio, so there is no reason to pay for length-prefix
    // framing on either side.
    private void handleConnection(LocalSocket client) throws IOException {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(client.getInputStream(), StandardCharsets.UTF_8));
        String line;
        while (running && (line = in.readLine()) != null) {
            try {
                JSONObject obj = new JSONObject(line);
                final int r = obj.optInt("r", 0);
                final int g = obj.optInt("g", 0);
                final int b = obj.optInt("b", 0);
                mainHandler.post(new Runnable() {
                    @Override public void run() { applyColor(r, g, b); }
                });
            } catch (org.json.JSONException e) {
                Log.w(TAG, "bad status line, ignoring: " + line);
            }
        }
    }

    // {0,0,0} means "off" (led_crown.go sends it for clearLeds and any
    // all-black scene) — hide the bar rather than draw a visible black line
    // over the screen at idle. Also the on/off edge that drives the wake
    // lock: a locked or sleeping screen shows nothing no matter how the bar
    // is drawn, so a wake word has to actually wake the screen for the bar
    // to be visible at all — see MAX_TURN_WAKE_MS for why this is a real
    // acquire/release pair and not a fire-and-forget flash.
    private void applyColor(int r, int g, int b) {
        boolean on = !(r == 0 && g == 0 && b == 0);
        if (on && !turnActive) {
            if (wakeLock != null) wakeLock.acquire(MAX_TURN_WAKE_MS);
        } else if (!on && turnActive) {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        }
        turnActive = on;

        if (topBarView == null) return;
        if (!on) {
            topBarView.setVisibility(View.GONE);
            return;
        }
        topBarView.setColor(Color.rgb(r, g, b));
        topBarView.setVisibility(View.VISIBLE);
    }

    private void addView() {
        if (topBarView != null) return;
        windowManager = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);

        int overlayType = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_SYSTEM_ALERT;

        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                dp(14),
                overlayType,
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP;

        topBarView = new TopBarView(context);
        topBarView.setVisibility(View.GONE);
        try {
            windowManager.addView(topBarView, params);
        } catch (WindowManager.BadTokenException | SecurityException e) {
            // SYSTEM_ALERT_WINDOW not granted yet (provision_crown.sh's
            // appops call hasn't run, or ran against the wrong package) —
            // log loudly rather than crash the service the real daemon
            // lives inside. The border just never appears; everything else
            // (mic, speaker, wake word) is unaffected.
            Log.e(TAG, "could not add overlay view — SYSTEM_ALERT_WINDOW not granted? " + e);
            topBarView = null;
        }
    }

    private int dp(int value) {
        float density = context.getResources().getDisplayMetrics().density;
        return Math.round(value * density);
    }

    private void removeView() {
        if (topBarView != null && windowManager != null) {
            try {
                windowManager.removeView(topBarView);
            } catch (IllegalArgumentException ignored) {
            }
        }
        topBarView = null;
    }

    /**
     * Paints a glowing horizontal bar filling the view — the view itself is
     * only 14dp tall and pinned to the top of the screen (see addView), so
     * "fill it" already IS "draw a bar along the top edge". The blur pushes
     * the glow up past the view's own bounds into the status-bar area and
     * down into the app below, which is what makes it read as a light
     * source rather than a flat coloured rectangle.
     *
     * FLAG_NOT_TOUCHABLE (set on the window, not here) means even a fully
     * opaque bar never intercepts a touch — it only ever draws.
     *
     * BlurMaskFilter needs a software-rendered layer — hardware
     * acceleration silently ignores mask filters on a View's Paint, which
     * would render as a crisp, unglowed line and looked like a config bug
     * before this was set explicitly.
     */
    private static class TopBarView extends View {
        // 130-255: a full fade to invisible reads as the bar switching off
        // and on rather than breathing, and made the pulse look like a
        // flicker rather than the slow "alive" cue LedRing's ledpulse
        // animation gives pending/offline states.
        private static final int PULSE_MIN_ALPHA = 130;
        private static final int PULSE_MAX_ALPHA = 255;
        private static final long PULSE_PERIOD_MS = 1800; // matches ledpulse's 1.8s

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF rect = new RectF();
        private final float glowRadius;
        private final ValueAnimator pulse;

        TopBarView(Context context) {
            super(context);
            float density = context.getResources().getDisplayMetrics().density;
            glowRadius = 14 * density;

            paint.setStyle(Paint.Style.FILL);
            paint.setMaskFilter(new BlurMaskFilter(glowRadius, BlurMaskFilter.Blur.NORMAL));
            paint.setColor(Color.TRANSPARENT);
            setLayerType(View.LAYER_TYPE_SOFTWARE, paint);

            pulse = ValueAnimator.ofInt(PULSE_MIN_ALPHA, PULSE_MAX_ALPHA);
            pulse.setDuration(PULSE_PERIOD_MS / 2);
            pulse.setRepeatMode(ValueAnimator.REVERSE);
            pulse.setRepeatCount(ValueAnimator.INFINITE);
            pulse.addUpdateListener(new ValueAnimator.AnimatorUpdateListener() {
                @Override public void onAnimationUpdate(ValueAnimator animation) {
                    paint.setAlpha((Integer) animation.getAnimatedValue());
                    invalidate();
                }
            });
        }

        void setColor(int color) {
            // setColor also sets full alpha; the animator's own alpha
            // updates take over on the next frame once it's (re)started, so
            // a colour change never sticks at a stale mid-pulse alpha.
            paint.setColor(color);
            invalidate();
        }

        @Override
        protected void onVisibilityChanged(View changed, int visibility) {
            super.onVisibilityChanged(changed, visibility);
            // Pulsing only while actually shown — an animator ticking away
            // (and invalidating) behind a GONE view wastes battery on a
            // device that spends most of its life idle.
            if (visibility == View.VISIBLE) {
                if (!pulse.isStarted()) pulse.start();
            } else {
                pulse.cancel();
                paint.setAlpha(PULSE_MAX_ALPHA);
            }
        }

        @Override
        protected void onSizeChanged(int w, int h, int oldw, int oldh) {
            super.onSizeChanged(w, h, oldw, oldh);
            // Fill the view's own bounds (its whole height IS the bar
            // thickness — see addView's dp(14) window height); the blur
            // radius pushes the visible glow beyond this rect on its own,
            // so nothing needs inset here the way the border version did.
            rect.set(0, 0, w, h);
        }

        @Override
        protected void onDraw(Canvas canvas) {
            canvas.drawRect(rect, paint);
        }
    }
}
