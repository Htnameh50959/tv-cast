package local.hemanth.tvcast;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;

/** Talks to an LG webOS TV on port 3000 to open the casting page. */
public final class Ssap {
    public interface Keys {
        String load();

        void save(String key);
    }

    public interface Listener {
        void onPrompt();

        void onLog(String message);
    }

    private static final String REGISTRATION_PAYLOAD = """
            {"forcePairing":false,"manifest":{"appVersion":"1.1","manifestVersion":1,"permissions":["LAUNCH","LAUNCH_WEBAPP","APP_TO_APP","CLOSE","TEST_OPEN","TEST_PROTECTED","CONTROL_AUDIO","CONTROL_DISPLAY","CONTROL_INPUT_JOYSTICK","CONTROL_INPUT_MEDIA_RECORDING","CONTROL_INPUT_MEDIA_PLAYBACK","CONTROL_INPUT_TV","CONTROL_POWER","CONTROL_TV_SCREEN","READ_APP_STATUS","READ_CURRENT_CHANNEL","READ_INPUT_DEVICE_LIST","READ_NETWORK_STATE","READ_RUNNING_APPS","READ_TV_CHANNEL_LIST","WRITE_NOTIFICATION_TOAST","READ_POWER_STATE","READ_COUNTRY_INFO","CONTROL_INPUT_TEXT","CONTROL_MOUSE_AND_KEYBOARD","READ_INSTALLED_APPS","READ_SETTINGS"],"signatures":[{"signature":"eyJhbGdvcml0aG0iOiJSU0EtU0hBMjU2Iiwia2V5SWQiOiJ0ZXN0LXNpZ25pbmctY2VydCIsInNpZ25hdHVyZVZlcnNpb24iOjF9.hrVRgjCwXVvE2OOSpDZ58hR+59aFNwYDyjQgKk3auukd7pcegmE2CzPCa0bJ0ZsRAcKkCTJrWo5iDzNhMBWRyaMOv5zWSrthlf7G128qvIlpMT0YNY+n/FaOHE73uLrS/g7swl3/qH/BGFG2Hu4RlL48eb3lLKqTt2xKHdCs6Cd4RMfJPYnzgvI4BNrFUKsjkcu+WD4OO2A27Pq1n50cMchmcaXadJhGrOqH5YmHdOCj5NSHzJYrsW0HPlpuAx/ECMeIZYDh6RMqaFM2DXzdKX9NmmyqzJ3o/0lkk/N97gfVRLW5hA29yeAwaCViZNCP8iC9aO0q9fQojoa7NQnAtw==","signatureVersion":1}],"signed":{"appId":"com.lge.test","created":"20140509","localizedAppNames":{"":"LG Remote App","ko-KR":"리모컨 앱","zxx-XX":"ЛГ Rэмotэ AПП"},"localizedVendorNames":{"":"LG Electronics"},"permissions":["TEST_SECURE","CONTROL_INPUT_TEXT","CONTROL_MOUSE_AND_KEYBOARD","READ_INSTALLED_APPS","READ_LGE_SDX","READ_NOTIFICATIONS","SEARCH","WRITE_SETTINGS","WRITE_NOTIFICATION_ALERT","CONTROL_POWER","READ_CURRENT_CHANNEL","READ_RUNNING_APPS","READ_UPDATE_INFO","UPDATE_FROM_REMOTE_APP","READ_LGE_TV_INPUT_EVENTS","READ_TV_CURRENT_TIME"],"serial":"2f930e2d2cfe083771f68e4fe7bb07","vendorId":"com.lge"}},"pairingType":"PROMPT"}
            """;

    public static final class Remote {
        private final String ip;
        private final Keys keys;
        private final Listener listener;

        public Remote(String ip, Keys keys, Listener listener) {
            this.ip = ip;
            this.keys = keys;
            this.listener = listener;
        }

        public void openUrl(String url) throws IOException {
            try (WebSocket socket = WebSocket.connect(ip, 3000, 5000)) {
                socket.receiveTimeout(60000);
                socket.sendText(registerMessage());
                waitForRegistered(socket);
                socket.receiveTimeout(10000);
                socket.sendText(new JSONObject()
                        .put("type", "request")
                        .put("id", "r0")
                        .put("uri", "ssap://system.launcher/open")
                        .put("payload", new JSONObject().put("target", url))
                        .toString());
                JSONObject reply = waitForReply(socket, "r0");
                if ("error".equals(reply.optString("type"))) {
                    throw new IOException(reply.optString("errorText", reply.toString()));
                }
                JSONObject payload = reply.optJSONObject("payload");
                if (payload != null && !payload.optBoolean("returnValue", true)) {
                    throw new IOException(payload.optString("errorText", reply.toString()));
                }
            } catch (JSONException e) {
                throw new IOException(e);
            }
        }

        public void closeBrowser() throws IOException {
            try (WebSocket socket = WebSocket.connect(ip, 3000, 5000)) {
                socket.receiveTimeout(60000);
                socket.sendText(registerMessage());
                waitForRegistered(socket);
                socket.receiveTimeout(10000);
                socket.sendText(new JSONObject()
                        .put("type", "request")
                        .put("id", "r1")
                        .put("uri", "ssap://system.launcher/close")
                        .put("payload", new JSONObject().put("id", "com.webos.app.browser"))
                        .toString());
                waitForReply(socket, "r1");
            } catch (JSONException e) {
                throw new IOException(e);
            }
        }

        private String registerMessage() throws JSONException {
            JSONObject payload = new JSONObject(REGISTRATION_PAYLOAD);
            String key = keys.load();
            if (key != null && !key.isEmpty()) {
                payload.put("client-key", key);
            }
            return new JSONObject()
                    .put("type", "register")
                    .put("id", "reg0")
                    .put("payload", payload)
                    .toString();
        }

        private void waitForRegistered(WebSocket socket) throws IOException {
            while (true) {
                String text = socket.receiveText();
                JSONObject message;
                try {
                    message = new JSONObject(text);
                } catch (JSONException e) {
                    continue;
                }
                String type = message.optString("type");
                if ("registered".equals(type)) {
                    String clientKey = message.optString("client-key", null);
                    if (clientKey != null && !clientKey.isEmpty()) {
                        keys.save(clientKey);
                    }
                    return;
                }
                if ("error".equals(type)) {
                    throw new IOException(message.optString("errorText", "TV refused the connection"));
                }
                JSONObject payload = message.optJSONObject("payload");
                if (payload != null && "PROMPT".equals(payload.optString("pairingType"))) {
                    listener.onPrompt();
                }
            }
        }

        private JSONObject waitForReply(WebSocket socket, String id) throws IOException {
            while (true) {
                String text = socket.receiveText();
                JSONObject message;
                try {
                    message = new JSONObject(text);
                } catch (JSONException e) {
                    continue;
                }
                if (id.equals(message.optString("id"))) {
                    return message;
                }
            }
        }
    }
}
