/*
 * Kitchen Queue - ESP32 uplink.
 *
 * The contract between any edge device and the server is one integer:
 *
 *     POST /api/ingest
 *     X-Device-Key: <shared secret>
 *     {"count": 14, "device": "kitchen-thermal-1"}
 *
 * So the counting method is entirely your choice. This sketch is the
 * transport half - Wi-Fi, TLS, retry, backlog - with a single function,
 * readQueueCount(), left for you to fill in.
 *
 * Two sensible things to put in it:
 *
 *   MLX90640 thermal array (32x24, overhead)
 *       Count connected blobs above ~30 C. Physically cannot identify
 *       anyone, which makes the approval conversation short. Library:
 *       Adafruit_MLX90640.
 *
 *   LD2450 mmWave radar
 *       Reports up to three tracked targets over UART. Good at movement,
 *       weak at people standing still - test it on a real queue before
 *       committing.
 *
 * What this deliberately does NOT do: listen on a port, run a web server,
 * or accept anything inbound. It only ever dials out. That is what lets it
 * live on a school network without a single firewall change.
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <time.h>

// ---------------------------------------------------------------- config

static const char *WIFI_SSID = "school-iot";
static const char *WIFI_PASS = "CHANGE_ME";

static const char *INGEST_URL = "https://queue.yourschool.example/api/ingest";
static const char *DEVICE_KEY = "CHANGE_ME_LONG_RANDOM_STRING";
static const char *DEVICE_NAME = "kitchen-thermal-1";

static const uint32_t POST_INTERVAL_MS = 10000;  // one sample per 10 s
static const uint32_t SAMPLE_INTERVAL_MS = 1000; // read the sensor per 1 s
static const float SMOOTHING = 0.35f;            // EMA alpha, as on the Pi

// Root CA for your server. Paste the ISRG Root X1 PEM here if you use
// Let's Encrypt. Leaving it null falls back to setInsecure() below, which
// is fine on a closed school network and not fine on the open internet.
static const char *ROOT_CA = nullptr;

// ------------------------------------------------------------- backlog

// School Wi-Fi drops. Rather than lose samples, hold them here and flush
// when the link returns - the history stays intact even though the live
// number went stale for a minute.
struct Sample {
  time_t ts;   // real epoch seconds, so a replayed sample lands in the past
  float count; // where it belongs instead of overwriting the live number
};

static const size_t BACKLOG_MAX = 60;
static Sample backlog[BACKLOG_MAX];
static size_t backlogCount = 0;

static void backlogPush(time_t ts, float count) {
  if (backlogCount == BACKLOG_MAX) {
    // Drop the oldest. A stale count is worth less than a fresh one.
    memmove(backlog, backlog + 1, sizeof(Sample) * (BACKLOG_MAX - 1));
    backlogCount--;
  }
  backlog[backlogCount++] = {ts, count};
}

// NTP, purely so buffered samples carry the time they were actually taken.
// The server rejects timestamps more than an hour from its own clock, so a
// board that never synced simply omits ts and is treated as "now".
static bool clockReady() { return time(nullptr) > 1700000000; }

// --------------------------------------------------------------- sensor

/*
 * Return the number of people currently in the queue, or -1 if the sensor
 * could not be read this cycle.
 *
 * Replace this. The stub returns a slow triangle wave so you can verify
 * the uplink, the server and the website before the sensor arrives.
 */
static float readQueueCount() {
  uint32_t phase = (millis() / 1000) % 120;
  return phase < 60 ? phase * 0.4f : (120 - phase) * 0.4f;
}

// --------------------------------------------------------------- uplink

static bool postCount(float count, time_t ts) {
  if (WiFi.status() != WL_CONNECTED) return false;

  WiFiClientSecure client;
  if (ROOT_CA) {
    client.setCACert(ROOT_CA);
  } else {
    client.setInsecure();  // see note on ROOT_CA above
  }

  HTTPClient http;
  if (!http.begin(client, INGEST_URL)) return false;

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.setTimeout(8000);

  char body[160];
  if (ts > 0) {
    snprintf(body, sizeof(body),
             "{\"count\":%.1f,\"device\":\"%s\",\"ts\":%lu}",
             count, DEVICE_NAME, (unsigned long)ts);
  } else {
    snprintf(body, sizeof(body), "{\"count\":%.1f,\"device\":\"%s\"}",
             count, DEVICE_NAME);
  }

  int status = http.POST((uint8_t *)body, strlen(body));
  http.end();

  if (status != 200) {
    Serial.printf("ingest failed: HTTP %d\n", status);
    return false;
  }
  return true;
}

static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.printf("connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  // Bounded wait: never block the sensor loop forever on a dead AP.
  uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? " ok" : " failed");
}

// ----------------------------------------------------------------- main

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\nkitchen queue uplink");
  ensureWifi();
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  // TODO: initialise your sensor here (mlx.begin(), Serial2.begin(), ...)
}

void loop() {
  static uint32_t lastSample = 0;
  static uint32_t lastPost = 0;
  static float ema = -1.0f;

  uint32_t now = millis();

  if (now - lastSample >= SAMPLE_INTERVAL_MS) {
    lastSample = now;
    float reading = readQueueCount();
    if (reading >= 0) {
      ema = (ema < 0) ? reading : SMOOTHING * reading + (1 - SMOOTHING) * ema;
    }
  }

  if (now - lastPost >= POST_INTERVAL_MS && ema >= 0) {
    lastPost = now;
    ensureWifi();

    time_t ts = clockReady() ? time(nullptr) : 0;

    if (postCount(ema, ts)) {
      // Link is up - drain what piled up while it was down. Each carries its
      // own timestamp, so a replayed sample lands in the history instead of
      // overwriting the live number. Samples taken before the clock synced
      // are undateable, so drop them rather than publish them as "now".
      while (backlogCount > 0) {
        if (backlog[0].ts > 0 && !postCount(backlog[0].count, backlog[0].ts)) break;
        memmove(backlog, backlog + 1, sizeof(Sample) * (--backlogCount));
      }
      Serial.printf("count=%.1f backlog=%u\n", ema, (unsigned)backlogCount);
    } else {
      backlogPush(ts, ema);
      Serial.printf("offline, buffered (backlog=%u)\n", (unsigned)backlogCount);
    }
  }

  delay(50);
}
