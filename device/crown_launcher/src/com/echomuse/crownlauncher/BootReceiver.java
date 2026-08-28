package com.echomuse.crownlauncher;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Fires on BOOT_COMPLETED and hands off to ServerService. This is the whole
 * reason this APK exists: it's ordinary app code, so it has none of raw
 * init's restriction on exec'ing untrusted /data binaries.
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        context.startForegroundService(new Intent(context, ServerService.class));
    }
}
