package local.hemanth.tvcast;

import android.app.Activity;
import android.media.projection.MediaProjectionManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.Spinner;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {
    private static final int REQ_PROJECTION = 1;
    private static final int REQ_NOTIFICATIONS = 2;
    private static final String PREFS = "tvcast";

    private static final String[] QUALITY_LABELS = {
            "1280×720 at 12 fps (recommended)",
            "1920×1080 at 10 fps",
            "960×540 at 15 fps (smoothest)",
    };
    private static final int[][] QUALITY = {
            {1280, 720, 12},
            {1920, 1080, 10},
            {960, 540, 15},
    };

    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<Ssdp.Tv> tvs = new ArrayList<>();

    private TextView status;
    private ProgressBar spinner;
    private LinearLayout list;
    private Button refresh;
    private Button stop;
    private Spinner quality;

    private Ssdp.Tv pendingTv;
    private boolean casting;
    private boolean scanning;
    private WifiManager.MulticastLock multicastLock;
    private BroadcastReceiver receiver;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        status = findViewById(R.id.status);
        spinner = findViewById(R.id.spinner);
        list = findViewById(R.id.tv_list);
        refresh = findViewById(R.id.refresh);
        stop = findViewById(R.id.stop);
        quality = findViewById(R.id.quality);

        quality.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, QUALITY_LABELS));
        quality.setSelection(0);

        refresh.setOnClickListener(v -> scan());
        stop.setOnClickListener(v -> stopCasting());

        receiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                String text = intent.getStringExtra(CastService.EXTRA_TEXT);
                boolean isCasting = intent.getBooleanExtra(CastService.EXTRA_CASTING, false);
                if (text != null) {
                    status.setText(text);
                }
                casting = isCasting;
                updateUi();
                if (!isCasting && pendingTv == null) {
                    // allow a rescan once casting ends
                }
            }
        };
        if (Build.VERSION.SDK_INT >= 31) {
            registerReceiver(receiver, new IntentFilter(CastService.ACTION_STATE), null, null, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(receiver, new IntentFilter(CastService.ACTION_STATE));
        }

        scan();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try {
            unregisterReceiver(receiver);
        } catch (Exception ignored) {
        }
        releaseMulticast();
    }

    private void scan() {
        if (scanning || casting) {
            return;
        }
        scanning = true;
        status.setText("Searching for TVs…");
        spinner.setVisibility(View.VISIBLE);
        refresh.setEnabled(false);
        acquireMulticast();
        new Thread(() -> {
            List<Ssdp.Tv> found = Ssdp.discover(3000);
            main.post(() -> showTvs(found));
        }, "tvcast-scan").start();
    }

    private void showTvs(List<Ssdp.Tv> found) {
        scanning = false;
        releaseMulticast();
        tvs.clear();
        tvs.addAll(found);

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String lastIp = prefs.getString("last_tv_ip", null);
        String lastName = prefs.getString("last_tv_name", "LG TV");
        if (lastIp != null && found.stream().noneMatch(t -> t.ip.equals(lastIp))) {
            tvs.add(new Ssdp.Tv(lastIp, lastName + " (last used)"));
        }

        list.removeAllViews();
        for (Ssdp.Tv tv : tvs) {
            list.addView(row(tv));
        }
        if (tvs.isEmpty()) {
            status.setText("No TV found yet. Turn the TV on and make sure it is on the same Wi-Fi.");
        } else {
            status.setText("Choose your TV.");
        }
        spinner.setVisibility(View.GONE);
        refresh.setEnabled(true);
    }

    private LinearLayout row(Ssdp.Tv tv) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, 12, 0, 12);

        TextView name = new TextView(this);
        name.setText(tv.name + "\n" + tv.ip);
        name.setTextSize(15f);
        LinearLayout.LayoutParams nameParams = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        name.setLayoutParams(nameParams);
        row.addView(name);

        Button cast = new Button(this);
        cast.setText(R.string.cast);
        cast.setOnClickListener(v -> cast(tv));
        row.addView(cast);
        return row;
    }

    private void cast(Ssdp.Tv tv) {
        pendingTv = tv;
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFICATIONS);
            return;
        }
        askProjection();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_NOTIFICATIONS) {
            askProjection();
        }
    }

    private void askProjection() {
        if (pendingTv == null) {
            return;
        }
        status.setText("Allow screen sharing…");
        MediaProjectionManager manager = (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        startActivityForResult(manager.createScreenCaptureIntent(), REQ_PROJECTION);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PROJECTION || pendingTv == null) {
            return;
        }
        if (resultCode != RESULT_OK || data == null) {
            status.setText("Screen sharing was not allowed.");
            pendingTv = null;
            return;
        }
        Ssdp.Tv tv = pendingTv;
        pendingTv = null;

        int[] q = QUALITY[quality.getSelectedItemPosition()];
        int[] size = targetSize(q[0], q[1]);
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        prefs.edit().putString("last_tv_ip", tv.ip).putString("last_tv_name", tv.name).apply();

        Intent service = new Intent(this, CastService.class)
                .setAction(CastService.ACTION_START)
                .putExtra(CastService.EXTRA_RESULT, resultCode)
                .putExtra(CastService.EXTRA_DATA, data)
                .putExtra(CastService.EXTRA_TV_IP, tv.ip)
                .putExtra(CastService.EXTRA_TV_NAME, tv.name)
                .putExtra(CastService.EXTRA_WIDTH, size[0])
                .putExtra(CastService.EXTRA_HEIGHT, size[1])
                .putExtra(CastService.EXTRA_FPS, q[2])
                .putExtra(CastService.EXTRA_QUALITY, 80);
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(service);
        } else {
            startService(service);
        }
        casting = true;
        updateUi();
        status.setText("Starting…");
    }

    private int[] targetSize(int maxW, int maxH) {
        android.util.DisplayMetrics dm = new android.util.DisplayMetrics();
        getWindowManager().getDefaultDisplay().getRealMetrics(dm);
        float scale = Math.min((float) maxW / dm.widthPixels, (float) maxH / dm.heightPixels);
        int w = Math.max(2, ((int) (dm.widthPixels * scale)) & ~1);
        int h = Math.max(2, ((int) (dm.heightPixels * scale)) & ~1);
        return new int[]{w, h};
    }

    private void stopCasting() {
        startService(new Intent(this, CastService.class).setAction(CastService.ACTION_STOP));
        casting = false;
        updateUi();
        status.setText("Stopping…");
    }

    private void updateUi() {
        stop.setVisibility(casting ? View.VISIBLE : View.GONE);
        refresh.setEnabled(!casting && !scanning);
    }

    private void acquireMulticast() {
        try {
            WifiManager wifi = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wifi != null) {
                multicastLock = wifi.createMulticastLock("tvcast");
                multicastLock.setReferenceCounted(true);
                multicastLock.acquire();
            }
        } catch (Exception ignored) {
        }
    }

    private void releaseMulticast() {
        try {
            if (multicastLock != null && multicastLock.isHeld()) {
                multicastLock.release();
            }
        } catch (Exception ignored) {
        }
        multicastLock = null;
    }
}
