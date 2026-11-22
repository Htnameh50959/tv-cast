package local.hemanth.tvcast;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.graphics.Point;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.IBinder;
import android.view.Display;
import android.view.WindowManager;

import java.io.IOException;

public class CastService extends Service {
    public static final String ACTION_START = "local.hemanth.tvcast.action.START";
    public static final String ACTION_STOP = "local.hemanth.tvcast.action.STOP";
    public static final String ACTION_STATE = "local.hemanth.tvcast.action.STATE";
    public static final String EXTRA_TEXT = "text";
    public static final String EXTRA_CASTING = "casting";
    public static final String EXTRA_RESULT = "result";
    public static final String EXTRA_DATA = "data";
    public static final String EXTRA_TV_IP = "tv_ip";
    public static final String EXTRA_TV_NAME = "tv_name";
    public static final String EXTRA_WIDTH = "width";
    public static final String EXTRA_HEIGHT = "height";
    public static final String EXTRA_FPS = "fps";
    public static final String EXTRA_QUALITY = "quality";

    private static final String CHANNEL = "casting";

    private MediaProjection projection;
    private ScreenCapture capture;
    private MjpegServer server;
    private volatile boolean casting;
    private String tvIp;
    private String tvName;
    private volatile boolean stopping;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            closeOnTv();
            stopSelf();
            return START_NOT_STICKY;
        }
        startForegroundNow();
        if (intent != null && intent.hasExtra(EXTRA_RESULT)) {
            startCast(intent);
        }
        return START_NOT_STICKY;
    }

    private void startCast(Intent intent) {
        if (capture != null) {
            return;
        }
        int result = intent.getIntExtra(EXTRA_RESULT, 0);
        Intent data = Build.VERSION.SDK_INT >= 33
                ? intent.getParcelableExtra(EXTRA_DATA, Intent.class)
                : intent.getParcelableExtra(EXTRA_DATA);
        tvIp = intent.getStringExtra(EXTRA_TV_IP);
        tvName = intent.getStringExtra(EXTRA_TV_NAME);
        int width = intent.getIntExtra(EXTRA_WIDTH, 1280);
        int height = intent.getIntExtra(EXTRA_HEIGHT, 720);
        int fps = intent.getIntExtra(EXTRA_FPS, 12);
        int quality = intent.getIntExtra(EXTRA_QUALITY, 80);

        MediaProjectionManager manager = (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        projection = manager.getMediaProjection(result, data);
        if (projection == null) {
            broadcast("Screen sharing was not allowed.");
            stopSelf();
            return;
        }

        String page = TvPage.html("this phone");
        server = new MjpegServer(page, 8090, new MjpegServer.Listener() {
            @Override
            public void onTvPicture(String ip) {
                if (!casting) {
                    casting = true;
                    broadcast("Your screen is on the TV.", true);
                }
            }

            @Override
            public void onTvLeft(String ip) {
                broadcast("The TV stopped showing the screen.", casting);
            }
        });
        try {
            server.start(30);
        } catch (IOException e) {
            broadcast("Could not start the video server: " + e.getMessage());
            stopSelf();
            return;
        }

        int dpi = getResources().getDisplayMetrics().densityDpi;
        capture = new ScreenCapture(projection, width, height, dpi, fps, quality, server,
                new ScreenCapture.Listener() {
                    @Override
                    public void onError(String message) {
                        broadcast(message);
                    }

                    @Override
                    public void onStop() {
                        broadcast("Screen sharing stopped.");
                        stopSelf();
                    }
                });
        capture.start();

        broadcast("Opened on the TV. Press OK on the TV remote if it asks.", true);
        saveLastTv();
        String url = "http://" + Ssdp.localIpTowards(tvIp, 8090) + ":8090/";
        new Thread(() -> openOnTv(url), "tvcast-ssap").start();
    }

    private void openOnTv(String url) {
        Ssap.Remote remote = new Ssap.Remote(tvIp, keys(), new Ssap.Listener() {
            @Override
            public void onPrompt() {
                broadcast(text(R.string.tv_prompt));
            }

            @Override
            public void onLog(String message) {
            }
        });
        try {
            remote.openUrl(url);
            broadcast("The TV's browser is opening. Press OK on the TV remote if it asks.", true);
        } catch (Exception e) {
            String text = e.getMessage() == null ? "" : e.getMessage().toLowerCase();
            if (text.contains("denied")) {
                broadcast("The TV said no. Cast again and press OK on the TV remote.");
            } else if (text.contains("refus") || text.contains("timed") || text.contains("connect")) {
                broadcast("Couldn't reach the TV. Make sure it is on and on the same Wi-Fi.");
            } else {
                broadcast("Couldn't open the page on the TV: " + e.getMessage());
            }
        }
    }

    private void closeOnTv() {
        if (tvIp == null) {
            return;
        }
        new Thread(() -> {
            try {
                new Ssap.Remote(tvIp, keys(), new Ssap.Listener() {
                    @Override
                    public void onPrompt() {
                    }

                    @Override
                    public void onLog(String message) {
                    }
                }).closeBrowser();
            } catch (Exception ignored) {
            }
        }, "tvcast-close").start();
    }

    private void broadcast(String text) {
        broadcast(text, casting);
    }

    private void broadcast(String text, boolean isCasting) {
        Intent i = new Intent(ACTION_STATE);
        i.setPackage(getPackageName());
        i.putExtra(EXTRA_TEXT, text);
        i.putExtra(EXTRA_CASTING, isCasting);
        sendBroadcast(i);
    }

    private Ssap.Keys keys() {
        SharedPreferences prefs = getSharedPreferences("tvcast", MODE_PRIVATE);
        return new Ssap.Keys() {
            @Override
            public String load() {
                return prefs.getString("client_key", "");
            }

            @Override
            public void save(String key) {
                prefs.edit().putString("client_key", key).apply();
            }
        };
    }

    private void saveLastTv() {
        SharedPreferences prefs = getSharedPreferences("tvcast", MODE_PRIVATE);
        prefs.edit().putString("last_tv_ip", tvIp).putString("last_tv_name", tvName).apply();
    }

    private void startForegroundNow() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.createNotificationChannel(new NotificationChannel(CHANNEL,
                        getString(R.string.notification_channel), NotificationManager.IMPORTANCE_LOW));
            }
        }
        Intent stop = new Intent(this, CastService.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent openPi = PendingIntent.getActivity(this, 2, open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        Notification.Builder builder = new Notification.Builder(this, CHANNEL);
        builder.setSmallIcon(R.drawable.ic_cast)
                .setContentTitle(text(R.string.notification_title))
                .setContentText(text(R.string.notification_text))
                .setContentIntent(openPi)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_menu_close_clear_cancel,
                        text(R.string.stop), stopPi).build());
        Notification notification = builder.build();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        } else {
            startForeground(1, notification);
        }
    }

    private String text(int id) {
        return getApplicationContext().getString(id);
    }

    @Override
    public void onDestroy() {
        stopping = true;
        casting = false;
        if (capture != null) {
            capture.stop();
            capture = null;
        }
        if (projection != null) {
            projection.stop();
            projection = null;
        }
        if (server != null) {
            server.stop();
            server = null;
        }
        broadcast("Casting stopped.", false);
        super.onDestroy();
    }
}
