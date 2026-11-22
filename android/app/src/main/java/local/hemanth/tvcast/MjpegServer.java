package local.hemanth.tvcast;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

/** Serves the casting page and the live MJPEG stream on one local port. */
public final class MjpegServer {
    public interface Listener {
        void onTvPicture(String ip);

        void onTvLeft(String ip);
    }

    private final String page;
    private final int port;
    private final Listener listener;
    private ServerSocket socket;
    private Thread acceptThread;
    private volatile boolean running;
    private byte[] frame;
    private long seq;
    private final Object lock = new Object();

    public MjpegServer(String page, int port, Listener listener) {
        this.page = page;
        this.port = port;
        this.listener = listener;
    }

    public void start(int attempts) throws IOException {
        IOException last = null;
        for (int i = 0; i < attempts; i++) {
            try {
                socket = new ServerSocket();
                socket.setReuseAddress(true);
                socket.bind(new InetSocketAddress(port));
                last = null;
                break;
            } catch (IOException e) {
                last = e;
                try {
                    Thread.sleep(300);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }
        if (socket == null || !socket.isBound()) {
            throw (last != null ? last : new IOException("Could not open port " + port));
        }
        running = true;
        acceptThread = new Thread(this::acceptLoop, "tvcast-http");
        acceptThread.setDaemon(true);
        acceptThread.start();
    }

    public void publish(byte[] jpeg) {
        synchronized (lock) {
            frame = jpeg;
            seq++;
            lock.notifyAll();
        }
    }

    public void stop() {
        running = false;
        synchronized (lock) {
            lock.notifyAll();
        }
        try {
            if (socket != null) {
                socket.close();
            }
        } catch (IOException ignored) {
        }
    }

    private void acceptLoop() {
        while (running) {
            Socket client;
            try {
                client = socket.accept();
            } catch (IOException e) {
                if (running) {
                    continue;
                }
                break;
            }
            Thread t = new Thread(() -> handle(client), "tvcast-client");
            t.setDaemon(true);
            t.start();
        }
    }

    private void handle(Socket client) {
        String ip = client.getInetAddress().getHostAddress();
        try {
            client.setSoTimeout(20000);
            BufferedReader in = new BufferedReader(
                    new InputStreamReader(client.getInputStream(), StandardCharsets.ISO_8859_1));
            String request = in.readLine();
            if (request == null) {
                return;
            }
            String[] parts = request.split(" ");
            if (parts.length < 2) {
                return;
            }
            String path = parts[1];
            String line;
            while ((line = in.readLine()) != null && !line.isEmpty()) {
                // Drain the headers.
            }
            if (path.startsWith("/mjpeg")) {
                stream(client, ip);
            } else if (path.startsWith("/hello")) {
                writeText(client, "ok");
                listener.onTvPicture(ip);
            } else {
                writeHtml(client, page);
            }
        } catch (IOException ignored) {
        } finally {
            try {
                client.close();
            } catch (IOException ignored) {
            }
        }
    }

    private void stream(Socket client, String ip) throws IOException {
        OutputStream out = client.getOutputStream();
        out.write(("HTTP/1.1 200 OK\r\n"
                + "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                + "Cache-Control: no-cache, no-store\r\n"
                + "Connection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        out.flush();

        long last = -1;
        boolean reported = false;
        try {
            while (running) {
                byte[] data;
                synchronized (lock) {
                    long deadline = System.currentTimeMillis() + 5000;
                    while (running && seq == last) {
                        long left = deadline - System.currentTimeMillis();
                        if (left <= 0) {
                            break;
                        }
                        try {
                            lock.wait(left);
                        } catch (InterruptedException e) {
                            Thread.currentThread().interrupt();
                            return;
                        }
                    }
                    data = frame;
                    last = seq;
                }
                if (data == null) {
                    continue;
                }
                out.write(("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + data.length + "\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
                out.write(data);
                out.write("\r\n".getBytes(StandardCharsets.US_ASCII));
                out.flush();
                if (!reported) {
                    reported = true;
                    listener.onTvPicture(ip);
                }
            }
        } finally {
            listener.onTvLeft(ip);
        }
    }

    private void writeText(Socket client, String text) throws IOException {
        byte[] body = text.getBytes(StandardCharsets.US_ASCII);
        OutputStream out = client.getOutputStream();
        out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                + "Content-Length: " + body.length + "\r\nConnection: close\r\n\r\n")
                .getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }

    private void writeHtml(Socket client, String html) throws IOException {
        byte[] body = html.getBytes(StandardCharsets.UTF_8);
        OutputStream out = client.getOutputStream();
        out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                + "Cache-Control: no-cache, no-store\r\n"
                + "Content-Length: " + body.length + "\r\nConnection: close\r\n\r\n")
                .getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }
}
