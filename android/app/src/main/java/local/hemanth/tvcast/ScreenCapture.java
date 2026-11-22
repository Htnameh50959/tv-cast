package local.hemanth.tvcast;

import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.util.Log;

import java.io.ByteArrayOutputStream;

public final class ScreenCapture {
    public interface Listener {
        void onError(String message);

        void onStop();
    }

    private final MediaProjection projection;
    private final int width;
    private final int height;
    private final int densityDpi;
    private final int fps;
    private final int quality;
    private final MjpegServer server;
    private final Listener listener;

    private ImageReader reader;
    private VirtualDisplay display;
    private HandlerThread thread;
    private Handler handler;
    private long lastFrame;
    private final long minGapMs;
    private boolean stopped;

    public ScreenCapture(MediaProjection projection, int width, int height, int densityDpi,
                         int fps, int quality, MjpegServer server, Listener listener) {
        this.projection = projection;
        this.width = width;
        this.height = height;
        this.densityDpi = densityDpi;
        this.fps = fps;
        this.quality = quality;
        this.server = server;
        this.listener = listener;
        this.minGapMs = 1000L / Math.max(1, fps);
    }

    public void start() {
        thread = new HandlerThread("tvcast-capture");
        thread.start();
        handler = new Handler(thread.getLooper());

        reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
        reader.setOnImageAvailableListener(this::onImageAvailable, handler);

        projection.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                if (!stopped) {
                    stopped = true;
                    listener.onStop();
                }
            }
        }, handler);

        display = projection.createVirtualDisplay("tvcast",
                width, height, densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                reader.getSurface(), null, handler);
    }

    public void stop() {
        if (stopped) {
            return;
        }
        stopped = true;
        try {
            if (display != null) {
                display.release();
            }
        } catch (Exception ignored) {
        }
        try {
            if (reader != null) {
                reader.close();
            }
        } catch (Exception ignored) {
        }
        try {
            if (thread != null) {
                thread.quitSafely();
            }
        } catch (Exception ignored) {
        }
    }

    private void onImageAvailable(ImageReader reader) {
        Image image = reader.acquireLatestImage();
        if (image == null || stopped) {
            if (image != null) {
                image.close();
            }
            return;
        }
        long now = SystemClock.elapsedRealtime();
        if (now - lastFrame < minGapMs) {
            image.close();
            return;
        }
        lastFrame = now;
        try {
            int rowStride = image.getPlanes()[0].getRowStride();
            int pixelStride = image.getPlanes()[0].getPixelStride();
            int paddedWidth = rowStride / Math.max(1, pixelStride);
            Bitmap padded = Bitmap.createBitmap(paddedWidth, height, Bitmap.Config.ARGB_8888);
            padded.copyPixelsFromBuffer(image.getPlanes()[0].getBuffer());
            Bitmap frame = padded;
            if (padded.getWidth() != width || padded.getHeight() != height) {
                frame = Bitmap.createBitmap(padded, 0, 0, width, height);
                padded.recycle();
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream(256 * 1024);
            frame.compress(Bitmap.CompressFormat.JPEG, quality, out);
            frame.recycle();
            server.publish(out.toByteArray());
        } catch (Exception e) {
            listener.onError("Could not read the screen: " + e.getMessage());
        } finally {
            image.close();
        }
    }
}
