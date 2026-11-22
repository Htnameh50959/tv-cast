package local.hemanth.tvcast;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public class CastReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String text = intent.getStringExtra(CastService.EXTRA_TEXT);
        boolean isCasting = intent.getBooleanExtra(CastService.EXTRA_CASTING, false);
        if (text != null) {
            // BroadcastReceiver cannot directly call Activity methods,
            // the MainActivity registers its own receiver dynamically or
            // uses LocalBroadcastManager. For now, just log the state.
            // The receiver in the manifest handles the broadcast for the app package.
        }
    }
}