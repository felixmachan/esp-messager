#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>
#include <WiFiManager.h>

static const char* AP_NAME = "ESP32-MSG";
static const char* MSG_URL = "http://165.227.135.68/msg?token=RANDOM123";
static const char* FRAME_URL_BASE = "http://165.227.135.68/gif/frame?token=RANDOM123";
static const uint32_t POLL_MS = 5000;

static const int FRAME_W = 128;
static const int FRAME_H = 160;
static const int MAX_FRAME_PIXELS = FRAME_W * FRAME_H;
static const int MAX_FRAME_BYTES = MAX_FRAME_PIXELS * 2;

TFT_eSPI tft = TFT_eSPI();
uint16_t frameBuffer[MAX_FRAME_PIXELS];

String lastText;

bool gifMode = false;
String gifId;
int gifFrameCount = 0;
int gifFrameDelayMs = 120;
int gifFrameIndex = 0;
uint32_t lastFrameMs = 0;

void drawLines(const String& l1, const String& l2 = "", const String& l3 = "") {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);
  tft.setCursor(0, 0);
  tft.println(l1);
  if (l2.length()) {
    tft.println(l2);
  }
  if (l3.length()) {
    tft.println(l3);
  }
}

void drawWrappedText(const String& text) {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);

  const int lineHeight = 16;
  const int maxLines = tft.height() / lineHeight;
  const int maxChars = 12;

  int line = 0;
  int idx = 0;
  while (idx < text.length() && line < maxLines) {
    int end = idx + maxChars;
    if (end > text.length()) {
      end = text.length();
    }

    String part = text.substring(idx, end);
    int breakPos = part.lastIndexOf(' ');
    if (end < text.length() && breakPos > 0) {
      part = part.substring(0, breakPos);
      end = idx + breakPos + 1;
    }

    tft.setCursor(0, line * lineHeight);
    tft.print(part);

    idx = end;
    while (idx < text.length() && text[idx] == ' ') {
      idx++;
    }
    line++;
  }
}

void onConfigPortal(WiFiManager* wm) {
  Serial.println("Entered WiFi config portal");
  Serial.printf("Config AP: %s\n", wm->getConfigPortalSSID().c_str());
  drawLines("WiFi setup", "AP: ESP32-MSG", "Open: 192.168.4.1");
}

bool fetchFrame(const String& id, int index) {
  const int frameBytes = MAX_FRAME_BYTES;
  String url = String(FRAME_URL_BASE) + "&gif_id=" + id + "&i=" + String(index);
  HTTPClient http;
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);

  if (!http.begin(url)) {
    Serial.println("Frame begin failed");
    return false;
  }

  int code = http.GET();
  Serial.printf("Frame HTTP code: %d idx=%d\n", code, index);
  if (code != 200) {
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  int totalRead = 0;
  uint32_t lastDataMs = millis();

  while (totalRead < frameBytes && (http.connected() || stream->available())) {
    int avail = stream->available();
    if (avail <= 0) {
      if (millis() - lastDataMs > 2500) {
        break;
      }
      delay(2);
      continue;
    }

    int need = frameBytes - totalRead;
    int toRead = (avail < need) ? avail : need;
    uint8_t* dst = reinterpret_cast<uint8_t*>(frameBuffer);
    int nowRead = stream->readBytes(dst + totalRead, toRead);
    if (nowRead > 0) {
      totalRead += nowRead;
      lastDataMs = millis();
    }
  }

  http.end();
  if (totalRead != frameBytes) {
    Serial.printf("Frame size mismatch: got=%d need=%d\n", totalRead, frameBytes);
    return false;
  }

  tft.pushImage(0, 0, FRAME_W, FRAME_H, frameBuffer);
  return true;
}

void showTextIfChanged(const String& text) {
  gifMode = false;
  if (text == lastText) {
    return;
  }
  lastText = text;
  drawWrappedText(text.length() ? text : "(empty)");
}

void updateGifState(const String& newGifId, int frameCount, int frameDelayMs) {
  if (newGifId == gifId && frameCount == gifFrameCount && frameDelayMs == gifFrameDelayMs) {
    gifMode = true;
    return;
  }

  gifId = newGifId;
  gifFrameCount = frameCount;
  gifFrameDelayMs = frameDelayMs;
  gifFrameIndex = 0;
  lastFrameMs = 0;
  gifMode = true;

  Serial.printf("GIF ready id=%s frames=%d delay=%d size=%dx%d\n", gifId.c_str(), gifFrameCount, gifFrameDelayMs, FRAME_W, FRAME_H);
  drawLines("GIF ready", "Streaming frames");
}

void pollMessage() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected");
    drawLines("WiFi", "Disconnected");
    return;
  }

  HTTPClient http;
  http.begin(MSG_URL);
  int httpCode = http.GET();
  Serial.printf("HTTP code: %d\n", httpCode);

  if (httpCode != 200) {
    drawLines("HTTP error", String(httpCode));
    http.end();
    return;
  }

  String body = http.getString();
  Serial.printf("Body: %s\n", body.c_str());

  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.printf("JSON parse error: %s\n", err.c_str());
    drawLines("JSON error");
    http.end();
    return;
  }

  String type = doc["type"] | "text";
  if (type == "gif") {
    String newGifId = doc["gif_id"] | "";
    int frameCount = doc["frame_count"] | 0;
    int frameDelay = doc["frame_delay_ms"] | 120;
    Serial.printf("Parsed gif id=%s frames=%d delay=%d size=%dx%d\n", newGifId.c_str(), frameCount, frameDelay, FRAME_W, FRAME_H);

    if (newGifId.length() == 0 || frameCount <= 0) {
      gifMode = false;
      drawLines("GIF invalid");
    } else {
      updateGifState(newGifId, frameCount, frameDelay);
    }
  } else {
    String text = doc["text"] | "";
    Serial.printf("Parsed text=%s\n", text.c_str());
    showTextIfChanged(text);
  }

  http.end();
}

void playGifFrameIfDue() {
  if (!gifMode || gifId.length() == 0 || gifFrameCount <= 0) {
    return;
  }

  uint32_t now = millis();
  if (now - lastFrameMs < (uint32_t)gifFrameDelayMs) {
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  bool ok = fetchFrame(gifId, gifFrameIndex);
  if (!ok) {
    Serial.printf("Frame fetch failed idx=%d\n", gifFrameIndex);
    drawLines("GIF frame err", String(gifFrameIndex));
    lastFrameMs = now;
    return;
  }

  gifFrameIndex++;
  if (gifFrameIndex >= gifFrameCount) {
    gifFrameIndex = 0;
  }
  lastFrameMs = now;
}

void setup() {
  Serial.begin(115200);
  delay(200);

  tft.init();
  tft.setRotation(1);
  Serial.printf("tft w=%d h=%d\n", tft.width(), tft.height());
  tft.setSwapBytes(true);

  tft.fillScreen(TFT_RED);
  delay(120);
  tft.fillScreen(TFT_GREEN);
  delay(120);
  tft.fillScreen(TFT_BLUE);
  delay(120);
  tft.fillScreen(TFT_BLACK);

  drawLines("Booting...");

  WiFi.mode(WIFI_STA);
  WiFiManager wm;
  wm.setAPCallback(onConfigPortal);
  wm.setConnectTimeout(20);
  wm.setConfigPortalTimeout(180);

  bool connected = wm.autoConnect(AP_NAME);
  if (!connected) {
    drawLines("WiFi failed", "Rebooting...");
    delay(1000);
    ESP.restart();
  }

  String ip = WiFi.localIP().toString();
  Serial.printf("WiFi OK, IP: %s\n", ip.c_str());
  drawLines("WiFi OK", ip);
  delay(800);
}

void loop() {
  static uint32_t lastPollMs = 0;
  uint32_t now = millis();

  if (now - lastPollMs >= POLL_MS) {
    pollMessage();
    lastPollMs = now;
  }

  playGifFrameIfDue();
}
