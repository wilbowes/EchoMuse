package com.echomuse.crownlauncher;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import java.io.File;
import java.io.IOException;

/**
 * Runs the real EchoMuse binary as a child process, as a foreground
 * service so Android doesn't kill it as background work.
 *
 * Deliberately as minimal as echomuse_crown.rc: exec the binary, log to
 * the same /data/local/tmp/echomuse.log path provision_crown.sh already
 * tails, START_STICKY so Android restarts the service (and therefore this
 * process) if it's killed. No log rotation, no supervisor, no A/B slots —
 * same reasoning as the .rc file: one dev unit, no fleet yet to need it.
 */
public class ServerService extends Service {
    private static final String TAG = "EchoMuseServerService";
    private static final String CHANNEL_ID = "echomuse_crown";
    private static final String BINARY = "/data/local/bin/server";
    private static final String LOG_FILE = "/data/local/tmp/echomuse.log";

    private Process proc;
    private volatile boolean stopping = false;
    private PlaybackServer playbackServer;
    private Thread playbackThread;
    private StatusOverlay statusOverlay;
    private Thread statusThread;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForegroundCompat();
        // Start the playback socket server before the daemon, so the
        // socket file exists by the time the daemon's first connect
        // attempt happens — see docs/echo-show-8-audiotrack-design.md.
        playbackServer = new PlaybackServer(this);
        playbackThread = new Thread(playbackServer, "echomuse-playback");
        playbackThread.start();
        // Same reasoning as the playback socket: bind before the daemon
        // starts so its first connect attempt lands on a listening socket.
        statusOverlay = new StatusOverlay(this);
        statusThread = new Thread(statusOverlay, "echomuse-status");
        statusThread.start();
        try {
            proc = new ProcessBuilder(BINARY)
                    .redirectErrorStream(true)
                    .redirectOutput(new File(LOG_FILE))
                    .start();
        } catch (IOException e) {
            stopSelf();
            return START_NOT_STICKY;
        }
        // Found in review 2026-08-27: this exec'd child was never watched.
        // START_STICKY only tells Android to restart the SERVICE if the OS
        // kills its process — it does nothing when the CHILD process this
        // service exists to run dies on its own (crash, `kill`, anything).
        // Without this, the service sits there looking alive (notification
        // still reads "running", the playback/overlay sockets still
        // listening for a daemon that's gone) with nothing restarting it
        // and nothing even logging that it died. waitFor() blocks, so it
        // runs on its own thread; stopSelf() on exit is what makes
        // START_STICKY's restart actually apply to the daemon again, not
        // just to this already-running service.
        final Process watchedProc = proc;
        new Thread(new Runnable() {
            @Override public void run() {
                int exitCode;
                try {
                    exitCode = watchedProc.waitFor();
                } catch (InterruptedException e) {
                    return;
                }
                if (stopping) return; // onDestroy()'s own proc.destroy() — expected
                Log.w(TAG, "daemon exited (code=" + exitCode + ") — restarting service "
                        + "so Android's START_STICKY brings it back");
                stopSelf();
            }
        }, "echomuse-daemon-watch").start();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        // Set BEFORE destroy() so the watcher thread's waitFor() (which
        // destroy() itself unblocks, normally, not via InterruptedException)
        // can tell "we did this on purpose" from "the daemon actually died"
        // and skip logging a restart that's already happening for another
        // reason.
        stopping = true;
        if (proc != null) {
            proc.destroy();
        }
        if (playbackServer != null) {
            playbackServer.stop();
        }
        if (statusOverlay != null) {
            statusOverlay.stop();
        }
        super.onDestroy();
    }

    private void startForegroundCompat() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(new NotificationChannel(
                    CHANNEL_ID, "EchoMuse", NotificationManager.IMPORTANCE_MIN));
        }
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("EchoMuse")
                .setContentText("running")
                .setSmallIcon(android.R.drawable.ic_media_play)
                .build();
        startForeground(1, notification);
    }
}
