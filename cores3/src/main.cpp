// FaceEcho 伙伴 · M5Stack CoreS3
// 通过 Wi-Fi 读 Thor 上的 /api/affect：画伙伴表情 + 台词 + 建议距离；
// 伙伴说出新的一句时，从 Thor 下载这一句的 WAV（static/voice/{lang}/{key}.wav）用喇叭播放。
// 触摸屏幕：静音 / 取消静音。
#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "config.h"
#ifndef ROTATION
// 屏幕方向 0~3（每 +1 顺时针转 90°）：1 = 横屏（出厂方向），0 = 逆时针转 90° 竖屏，3 = 顺时针转 90° 竖屏，2 = 横屏倒置
// 也可以在 config.h 里写 #define ROTATION 1 覆盖这里
#define ROTATION 0
#endif

static M5Canvas canvas(&M5.Display);

// ---------- 来自 Thor 的状态 ----------
struct State {
  bool ok = false;          // Thor 有数据
  String mood = "neutral";  // happy / surprise / sad / angry / neutral
  String text;              // 伙伴台词（已去掉 emoji）
  String lang = "ja";
  String line;              // 台词编号，如 happy_2
  long sayId = -1;          // 每说一句新话 +1
  float dist = NAN;         // 建议距离（米）
  bool observed = true;
  float age = 99;           // 数据有多旧（秒）
} st;

static long playedId = -1;
static bool muted = false;
static uint8_t* wavBuf = nullptr;
static size_t wavCap = 0;
static uint32_t lastPoll = 0;
static float t = 0;         // 动画时间

static String base() { return String("http://") + THOR_HOST + ":" + THOR_PORT; }

// ---------- 颜色（与网页里的伙伴一致） ----------
static uint16_t rgb(uint32_t c) { return M5.Display.color565((c >> 16) & 255, (c >> 8) & 255, c & 255); }
struct Look { const char* react; uint32_t bg, body, accent; };
static Look lookFor(const String& m) {
  if (m == "happy")    return {"happy",    0x2e3a2a, 0xffe08a, 0xffcf5a};
  if (m == "surprise") return {"surprise", 0x26314a, 0xa9d4ff, 0x7cc4ff};
  if (m == "sad")      return {"comfort",  0x2a2f4a, 0xb9c6ff, 0x9db4ff};
  if (m == "angry")    return {"calm",     0x3a2730, 0xffc2cf, 0xff9db0};
  return                      {"idle",     0x20233d, 0xa9e6c9, 0x8fe6c0};
}

// ---------- 网络 ----------
static void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setFont(&fonts::efontJA_16);
  M5.Display.setTextDatum(middle_center);
  M5.Display.drawString("Wi-Fi 接続中…", M5.Display.width() / 2, M5.Display.height() / 2 - 12);
  M5.Display.drawString(WIFI_SSID, M5.Display.width() / 2, M5.Display.height() / 2 + 12);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(200);
}

static void poll() {
  if (WiFi.status() != WL_CONNECTED) { st.ok = false; WiFi.reconnect(); return; }
  HTTPClient http;
  http.setTimeout(400);
  http.begin(base() + "/api/affect");
  int code = http.GET();
  if (code != 200) { st.ok = false; http.end(); return; }
  JsonDocument d;
  if (deserializeJson(d, http.getString())) { st.ok = false; http.end(); return; }
  http.end();
  st.ok = d["ok"] | false;
  st.age = d["age_s"] | 99.0f;
  st.dist = d["dist"] | NAN;
  st.observed = d["observed"] | true;
  if (!d["comp_mood"].isNull()) st.mood = d["comp_mood"].as<String>();
  if (!d["say_id"].isNull()) {
    st.sayId = d["say_id"] | -1L;
    st.text = d["say_text"] | "";
    st.lang = d["say_lang"] | "ja";
    st.line = d["say_line"] | "";
  }
}

// 下载一句 WAV 到 PSRAM 并播放
static void speak() {
  if (st.line.isEmpty()) return;
  HTTPClient http;
  http.setTimeout(3000);
  http.begin(base() + "/voice/" + st.lang + "/" + st.line + ".wav");
  int code = http.GET();
  if (code != 200) { Serial.printf("wav %d\n", code); http.end(); return; }
  int len = http.getSize();
  if (len <= 44 || len > 2 * 1024 * 1024) { http.end(); return; }
  M5.Speaker.stop();
  if ((size_t)len > wavCap) {
    if (wavBuf) free(wavBuf);
    wavBuf = (uint8_t*)ps_malloc(len); wavCap = wavBuf ? len : 0;
    if (!wavBuf) { http.end(); return; }
  }
  WiFiClient* s = http.getStreamPtr();
  size_t got = 0; uint32_t t0 = millis();
  while (got < (size_t)len && millis() - t0 < 3000) {
    size_t n = s->readBytes(wavBuf + got, len - got);
    if (n == 0) delay(2);
    got += n;
  }
  http.end();
  if (got == (size_t)len) M5.Speaker.playWav(wavBuf, len);
}

// ---------- 画伙伴（缩小版，与网页同风格） ----------
static void eyeRound(int x, int y, int r, bool blink) {
  if (blink) canvas.fillRoundRect(x - r, y - 2, r * 2, 4, 2, rgb(0x2b2b3a));
  else { canvas.fillCircle(x, y, r, rgb(0x2b2b3a)); canvas.fillCircle(x + r / 3, y - r / 3, r / 3, TFT_WHITE); }
}
static void thickArc(int cx, int cy, int r, int w, float a0, float a1) {  // a 以度为单位，0=右，顺时针
  canvas.fillArc(cx, cy, r, r - w, a0, a1, rgb(0x2b2b3a));
}

static void drawFace() {
  Look L = lookFor(st.ok ? st.mood : "neutral");
  String react = L.react;
  canvas.fillScreen(rgb(L.bg));
  const int W = canvas.width(), H = canvas.height();
  const bool portrait = H > W;
  int cx = W / 2, cy = (portrait ? H * 34 / 100 : 92) + (int)(sinf(t * 2) * 3), R = portrait ? 70 : 62;
  // 天线 + 身体
  canvas.drawLine(cx, cy - R, cx, cy - R - 14, rgb(L.body));
  canvas.drawLine(cx + 1, cy - R, cx + 1, cy - R - 14, rgb(L.body));
  canvas.fillCircle(cx, cy - R - 17, 4 + (int)(sinf(t * 3) * 1), TFT_WHITE);
  canvas.fillCircle(cx, cy, R, rgb(L.body));
  bool blink = sinf(t * 1.3f) > 0.98f;
  int ey = cy - 9, dx = 24;
  if (react == "happy") {
    thickArc(cx - dx, ey + 4, 10, 4, 200, 340); thickArc(cx + dx, ey + 4, 10, 4, 200, 340);
    thickArc(cx, cy + 10, 24, 5, 25, 155);
  } else if (react == "surprise") {
    eyeRound(cx - dx, ey, 9, blink); eyeRound(cx + dx, ey, 9, blink);
    canvas.fillEllipse(cx, cy + 18, 9, 11, rgb(0x2b2b3a));
    canvas.setFont(&fonts::FreeSansBold18pt7b); canvas.setTextColor(rgb(0xffd76c));
    canvas.setTextDatum(middle_center); canvas.drawString("!", cx + R + 6, cy - R + 14);
  } else if (react == "comfort") {
    canvas.drawWideLine(cx - dx - 9, ey - 9, cx - dx + 8, ey - 4, 3, rgb(0x2b2b3a));
    canvas.drawWideLine(cx + dx + 9, ey - 9, cx + dx - 8, ey - 4, 3, rgb(0x2b2b3a));
    eyeRound(cx - dx, ey + 2, 5, false); eyeRound(cx + dx, ey + 2, 5, false);
    thickArc(cx, cy + 12, 16, 4, 20, 160);
    canvas.fillCircle(cx + R - 6, cy - R + 18, 5, rgb(0xff7a95)); canvas.fillCircle(cx + R + 2, cy - R + 18, 5, rgb(0xff7a95));
    canvas.fillTriangle(cx + R - 11, cy - R + 20, cx + R + 7, cy - R + 20, cx + R - 2, cy - R + 30, rgb(0xff7a95));
  } else if (react == "calm") {
    thickArc(cx - dx, ey - 4, 8, 4, 20, 160); thickArc(cx + dx, ey - 4, 8, 4, 20, 160);
    canvas.drawWideLine(cx - 14, cy + 18, cx + 14, cy + 18, 3, rgb(0x2b2b3a));
    canvas.fillEllipse(cx + dx + 14, ey + 4, 3, 5, rgb(0x7cc4ff));
  } else {
    eyeRound(cx - dx, ey, 6, blink); eyeRound(cx + dx, ey, 6, blink);
    thickArc(cx, cy + 12, 13, 4, 25, 155);
  }
  if (react == "happy" || react == "comfort") {
    canvas.fillCircle(cx - 36, cy + 9, 8, rgb(0xff9aa8)); canvas.fillCircle(cx + 36, cy + 9, 8, rgb(0xff9aa8));
  }

  // 台词气泡
  const int bubY = portrait ? H * 66 / 100 : 170, bubH = portrait ? 56 : 34;
  canvas.fillRoundRect(8, bubY, W - 16, bubH, 8, rgb(0x15172a));
  canvas.drawRoundRect(8, bubY, W - 16, bubH, 8, rgb(L.accent));
  canvas.setFont(st.lang == "zh" ? &fonts::efontCN_16 : &fonts::efontJA_16);
  canvas.setTextColor(rgb(L.accent)); canvas.setTextDatum(middle_center);
  String line = !st.ok ? String("Thor ") + (WiFi.status() == WL_CONNECTED ? "offline" : "Wi-Fi ×") : st.text;
  canvas.setTextSize(1.0f);
  int tw = canvas.textWidth(line), avail = W - 28;
  if (tw > avail && portrait && tw <= avail * 2) {          // 竖屏：拆成两行
    int cut = line.length() / 2;
    while (cut < (int)line.length() && (line[cut] & 0xC0) == 0x80) cut++;   // 不切断 UTF-8 字符
    canvas.drawString(line.substring(0, cut), W / 2, bubY + bubH / 2 - 10);
    canvas.drawString(line.substring(cut), W / 2, bubY + bubH / 2 + 10);
  } else {
    if (tw > avail) canvas.setTextSize(max(0.6f, avail / (float)tw));      // 太长就缩小
    canvas.drawString(line, W / 2, bubY + bubH / 2);
    canvas.setTextSize(1.0f);
  }

  // 建议距离条 0.3 ~ 2.5 m
  canvas.setFont(&fonts::Font2); canvas.setTextColor(TFT_WHITE); canvas.setTextDatum(middle_left);
  int bx = 70, bw = W - 120, by = H - 18;
  for (int i = 0; i < bw; i++) {           // 红→黄→绿→青 渐变
    float f = i / (float)bw;
    uint8_t r = f < .45f ? 255 - (uint8_t)(f / .45f * 161) : 94 - (uint8_t)((f - .45f) / .55f * 86);
    uint8_t g = f < .22f ? 90 + (uint8_t)(f / .22f * 117) : 207 + (uint8_t)((f - .22f) / .78f * 33);
    uint8_t b = f < .45f ? 122 - (uint8_t)(f / .45f * 32) : 160 + (uint8_t)((f - .45f) / .55f * 95);
    canvas.drawFastVLine(bx + i, by - 4, 8, M5.Display.color565(r, g, b));
  }
  if (st.ok && !isnan(st.dist)) {
    int mx = bx + (int)(constrain((st.dist - 0.3f) / 2.2f, 0.0f, 1.0f) * bw);
    canvas.fillRect(mx - 2, by - 8, 4, 16, TFT_WHITE);
    canvas.drawString(String(st.dist, 2) + "m", 8, by);
  } else canvas.drawString("-- m", 8, by);
  canvas.setTextDatum(middle_right);
  canvas.setTextColor(st.observed ? rgb(0x5ee6a0) : rgb(0xffcf5a));
  canvas.drawString(muted ? "MUTE" : (st.observed ? "face" : "extrap"), W - 4, by);
  canvas.pushSprite(0, 0);
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  Serial.begin(115200);
  M5.Display.setRotation(ROTATION);
  M5.Speaker.begin();
  M5.Speaker.setVolume(VOLUME);
  canvas.setPsram(true);
  canvas.createSprite(M5.Display.width(), M5.Display.height());
  connectWiFi();
  Serial.printf("IP %s\n", WiFi.localIP().toString().c_str());
}

void loop() {
  M5.update();
  if (M5.Touch.getCount() && M5.Touch.getDetail().wasPressed()) {
    muted = !muted;
    if (muted) M5.Speaker.stop();
  }
  if (millis() - lastPoll > POLL_MS) {
    lastPoll = millis();
    poll();
    if (st.ok && st.age > STALE_S) st.ok = false;   // 网页没在推送
    if (st.ok && st.sayId != playedId) {
      bool first = playedId < 0;                    // 刚开机不补播旧台词
      playedId = st.sayId;
      if (!first && !muted) speak();
    }
  }
  t += 0.05f;
  drawFace();
  delay(30);
}
