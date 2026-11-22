package local.hemanth.tvcast;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.Closeable;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Base64;

/** Just enough of a WebSocket client to talk to an LG TV's control port (3000). */
final class WebSocket implements Closeable {
    private final Socket socket;
    private final InputStream in;
    private final OutputStream out;
    private final SecureRandom random = new SecureRandom();

    private WebSocket(Socket socket, InputStream in, OutputStream out) {
        this.socket = socket;
        this.in = in;
        this.out = out;
    }

    static WebSocket connect(String host, int port, int timeoutMs) throws IOException {
        Socket socket = new Socket();
        socket.connect(new InetSocketAddress(host, port), timeoutMs);
        socket.setSoTimeout(timeoutMs);
        socket.setTcpNoDelay(true);

        byte[] nonce = new byte[16];
        new SecureRandom().nextBytes(nonce);
        String key = Base64.getEncoder().encodeToString(nonce);
        String request = "GET / HTTP/1.1\r\n"
                + "Host: " + host + ":" + port + "\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Key: " + key + "\r\n"
                + "Sec-WebSocket-Version: 13\r\n\r\n";

        OutputStream out = new BufferedOutputStream(socket.getOutputStream());
        out.write(request.getBytes(StandardCharsets.US_ASCII));
        out.flush();

        InputStream in = new BufferedInputStream(socket.getInputStream());
        String status = readLine(in);
        if (!status.contains("101")) {
            socket.close();
            throw new IOException("TV refused the connection: " + status.trim());
        }
        while (!readLine(in).isEmpty()) {
            // Skip the handshake headers.
        }
        return new WebSocket(socket, in, out);
    }

    void receiveTimeout(int ms) throws IOException {
        socket.setSoTimeout(ms);
    }

    String receiveText() throws IOException {
        StringBuilder message = new StringBuilder();
        while (true) {
            int first = readByte();
            int second = readByte();
            boolean fin = (first & 0x80) != 0;
            int opcode = first & 0x0f;
            boolean masked = (second & 0x80) != 0;
            long length = second & 0x7f;
            if (length == 126) {
                length = (readByte() << 8) | readByte();
            } else if (length == 127) {
                length = 0;
                for (int i = 0; i < 8; i++) {
                    length = (length << 8) | readByte();
                }
            }
            if (length > 8L * 1024 * 1024) {
                throw new IOException("TV sent an oversized message");
            }
            byte[] mask = null;
            if (masked) {
                mask = new byte[4];
                readFully(mask);
            }
            byte[] data = new byte[(int) length];
            readFully(data);
            if (mask != null) {
                for (int i = 0; i < data.length; i++) {
                    data[i] ^= mask[i & 3];
                }
            }
            if (opcode == 8) {
                throw new IOException("TV closed the connection");
            }
            if (opcode == 9) {
                sendFrame(10, data);
                continue;
            }
            if (opcode == 10) {
                continue;
            }
            message.append(new String(data, StandardCharsets.UTF_8));
            if (fin) {
                return message.toString();
            }
        }
    }

    void sendText(String text) throws IOException {
        sendFrame(1, text.getBytes(StandardCharsets.UTF_8));
    }

    private void sendFrame(int opcode, byte[] data) throws IOException {
        out.write(0x80 | opcode);
        if (data.length < 126) {
            out.write(0x80 | data.length);
        } else if (data.length < 65536) {
            out.write(0x80 | 126);
            out.write((data.length >> 8) & 0xff);
            out.write(data.length & 0xff);
        } else {
            out.write(0x80 | 127);
            for (int i = 7; i >= 0; i--) {
                out.write((int) (((long) data.length >> (8 * i)) & 0xff));
            }
        }
        byte[] mask = new byte[4];
        random.nextBytes(mask);
        out.write(mask);
        byte[] masked = new byte[data.length];
        for (int i = 0; i < data.length; i++) {
            masked[i] = (byte) (data[i] ^ mask[i & 3]);
        }
        out.write(masked);
        out.flush();
    }

    private int readByte() throws IOException {
        int b = in.read();
        if (b < 0) {
            throw new EOFException("TV closed the connection");
        }
        return b;
    }

    private void readFully(byte[] buffer) throws IOException {
        int offset = 0;
        while (offset < buffer.length) {
            int n = in.read(buffer, offset, buffer.length - offset);
            if (n < 0) {
                throw new EOFException("TV closed the connection");
            }
            offset += n;
        }
    }

    private static String readLine(InputStream in) throws IOException {
        StringBuilder line = new StringBuilder();
        while (true) {
            int b = in.read();
            if (b < 0) {
                throw new EOFException("TV closed the connection");
            }
            if (b == '\n') {
                break;
            }
            if (b != '\r') {
                line.append((char) b);
            }
        }
        return line.toString();
    }

    @Override
    public void close() {
        try {
            socket.close();
        } catch (IOException ignored) {
        }
    }
}
