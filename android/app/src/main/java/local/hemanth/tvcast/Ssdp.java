package local.hemanth.tvcast;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Finds LG webOS TVs on the home Wi-Fi with SSDP. */
public final class Ssdp {
    private Ssdp() {
    }

    public static final class Tv {
        public final String ip;
        public final String name;

        public Tv(String ip, String name) {
            this.ip = ip;
            this.name = name;
        }

        @Override
        public String toString() {
            return name;
        }
    }

    public static List<Tv> discover(int timeoutMs) {
        Map<String, String> locations = new LinkedHashMap<>();
        byte[] search = (
                "M-SEARCH * HTTP/1.1\r\n"
                        + "HOST: 239.255.255.250:1900\r\n"
                        + "MAN: \"ssdp:discover\"\r\n"
                        + "MX: 2\r\n"
                        + "ST: urn:lge-com:service:webos-second-screen:1\r\n\r\n")
                .getBytes(StandardCharsets.US_ASCII);
        byte[] searchAll = new String(search, StandardCharsets.US_ASCII)
                .replace("urn:lge-com:service:webos-second-screen:1", "ssdp:all")
                .getBytes(StandardCharsets.US_ASCII);

        try (DatagramSocket socket = new DatagramSocket()) {
            socket.setSoTimeout(400);
            InetAddress group = InetAddress.getByName("239.255.255.250");
            for (int round = 0; round < 3; round++) {
                socket.send(new DatagramPacket(search, search.length, group, 1900));
                socket.send(new DatagramPacket(searchAll, searchAll.length, group, 1900));
            }
            long end = System.currentTimeMillis() + timeoutMs;
            byte[] buffer = new byte[4096];
            while (System.currentTimeMillis() < end) {
                DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                try {
                    socket.receive(packet);
                } catch (SocketTimeoutException e) {
                    continue;
                }
                String reply = new String(packet.getData(), 0, packet.getLength(), StandardCharsets.ISO_8859_1);
                String kind = (header(reply, "SERVER") + header(reply, "ST")).toLowerCase(Locale.ROOT);
                if (!kind.contains("webos")) {
                    continue;
                }
                String ip = packet.getAddress().getHostAddress();
                if (!locations.containsKey(ip)) {
                    locations.put(ip, header(reply, "LOCATION"));
                }
            }
        } catch (IOException ignored) {
            // No network, or multicast is blocked: the list simply stays empty.
        }

        List<Tv> tvs = new ArrayList<>();
        for (Map.Entry<String, String> entry : locations.entrySet()) {
            String name = "LG TV";
            try {
                String friendly = friendlyName(entry.getValue());
                if (friendly != null) {
                    name = friendly;
                }
            } catch (IOException ignored) {
            }
            tvs.add(new Tv(entry.getKey(), name));
        }
        return tvs;
    }

    /** The address of this phone on the network the TV can reach. */
    public static String localIpTowards(String host, int port) {
        try (DatagramSocket socket = new DatagramSocket()) {
            socket.connect(new InetSocketAddress(host, port));
            return socket.getLocalAddress().getHostAddress();
        } catch (IOException e) {
            return null;
        }
    }

    private static String friendlyName(String location) throws IOException {
        if (location == null || location.isEmpty()) {
            return null;
        }
        HttpURLConnection connection = (HttpURLConnection) new URL(location).openConnection();
        connection.setConnectTimeout(2000);
        connection.setReadTimeout(2000);
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(connection.getInputStream(), StandardCharsets.UTF_8))) {
            StringBuilder xml = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                xml.append(line);
            }
            String text = xml.toString();
            int start = text.indexOf("<friendlyName>");
            int end = text.indexOf("</friendlyName>");
            if (start >= 0 && end > start) {
                return text.substring(start + "<friendlyName>".length(), end).trim();
            }
        } finally {
            connection.disconnect();
        }
        return null;
    }

    private static String header(String reply, String name) {
        for (String line : reply.split("\r\n")) {
            int colon = line.indexOf(':');
            if (colon > 0 && line.substring(0, colon).trim().equalsIgnoreCase(name)) {
                return line.substring(colon + 1).trim();
            }
        }
        return "";
    }
}
