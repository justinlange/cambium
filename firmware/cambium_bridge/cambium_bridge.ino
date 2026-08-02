// cambium_bridge -- dumb radio modem between cambium (USB serial) and the
// Resonance Tree's ESP-NOW lantern fleet.
//
// It relays COBS-framed serial frames <-> ESP-NOW broadcast and knows NOTHING
// about Nb packet internals: packet.h can evolve forever without reflashing
// this bridge. The serial contract is defined in cambium/wire/framing.py +
// cobs.py (frame = COBS([ftype][payload][crc16 LE]) + 0x00 delimiter).
//
// USB-powered bench device: no PowerFeather SDK, no battery code.

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <stdarg.h>
#include <string.h>

#include "cobs.h"

#ifndef CB_CHANNEL
#define CB_CHANNEL 11 // must match the fleet channel or the bridge hears nothing
#endif
#define CB_FW_STR "cambium-br-0.1" // <= 15 chars: STATUS fw[16] must keep its NUL

// ---- serial contract constants (cambium/wire/framing.py) -------------------
static const uint8_t FTYPE_RADIO_TX = 0x01; // host -> bridge: raw Nb packet
static const uint8_t FTYPE_RADIO_RX = 0x02; // bridge -> host: mac[6]+rssi+raw
static const uint8_t FTYPE_CTRL = 0x03;
static const uint8_t FTYPE_STATUS = 0x04;
static const uint8_t FTYPE_LOG = 0x05;
static const uint8_t CTRL_STATUS_REQ = 0x01;
static const uint8_t CTRL_SET_CHANNEL = 0x02;
static const uint8_t CTRL_REBOOT = 0x03;

static const size_t ESPNOW_MAX = 250;          // esp-now payload hard limit
static const size_t RX_PAYLOAD_MAX = 6 + 1 + ESPNOW_MAX; // mac + rssi + raw
static const size_t BODY_MAX = 1 + RX_PAYLOAD_MAX + 2;   // ftype + payload + crc

static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ---- state -----------------------------------------------------------------
static volatile uint32_t gTxOk = 0, gTxFail = 0;   // from send callback
static volatile uint32_t gRxPkts = 0, gRxDrop = 0; // from recv callback
static uint16_t gCrcErr = 0;   // serial frames dropped (COBS or CRC damage)
static uint8_t gChannel = CB_CHANNEL; // RAM only; reboot returns to CB_CHANNEL

// STATUS payload, packed little-endian to match framing.parse_status
// ("<B6sBIIIIIH16s", 46 bytes; append-only -- add new fields at the end).
struct __attribute__((packed)) BridgeStatus {
  uint8_t proto; // = 1
  uint8_t mac[6];
  uint8_t channel;
  uint32_t uptime_ms;
  uint32_t tx_ok;
  uint32_t tx_fail;
  uint32_t rx_pkts;
  uint32_t rx_drop;
  uint16_t crc_err;
  char fw[16]; // zero-padded
};
static_assert(sizeof(BridgeStatus) == 46, "STATUS layout drifted from framing.py");

// ---- esp-now rx ring: 32 slots, SPSC ---------------------------------------
// Producer is the esp-now recv callback (WiFi task context), consumer is
// loop(); volatile head/tail indices make the single-producer/single-consumer
// ring safe without locks. Pattern after espnow_link.cpp's ISR-enqueue queue.
struct RxSlot {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[ESPNOW_MAX];
};
static RxSlot gRing[32];
static volatile uint8_t gHead = 0, gTail = 0; // ring indices, mod 32

static void enqueueRx(const uint8_t *mac, int8_t rssi, const uint8_t *data, int len) {
  if (len <= 0 || len > (int)ESPNOW_MAX) return;
  uint8_t next = (gHead + 1) & 31;
  if (next == gTail) { gRxDrop = gRxDrop + 1; return; } // ring full
  RxSlot &s = gRing[gHead];
  memcpy(s.mac, mac, 6);
  s.rssi = rssi;
  s.len = (uint8_t)len;
  memcpy(s.data, data, len);
  gHead = next;
  gRxPkts = gRxPkts + 1;
}

// ---- esp-now callbacks (signatures differ across esp32 cores) --------------
#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onEspNowRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  enqueueRx(info->src_addr, info->rx_ctrl ? info->rx_ctrl->rssi : 0, data, len);
}
#else
// esp32 core 2.x (IDF 4.4): recv cb carries no rx_ctrl, so rssi reads 0.
static void onEspNowRecv(const uint8_t *mac, const uint8_t *data, int len) {
  enqueueRx(mac, 0, data, len);
}
#endif

#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 3, 0)
static void onEspNowSend(const esp_now_send_info_t *, esp_now_send_status_t status) {
#else
static void onEspNowSend(const uint8_t *, esp_now_send_status_t status) {
#endif
  if (status == ESP_NOW_SEND_SUCCESS) gTxOk = gTxOk + 1;
  else gTxFail = gTxFail + 1;
}

// ---- serial TX: [ftype][payload][crc16 LE] -> COBS -> + 0x00 ---------------
static void sendFrame(uint8_t ftype, const uint8_t *payload, size_t len) {
  uint8_t body[BODY_MAX];
  uint8_t enc[COBS_ENCODE_MAX(BODY_MAX)];
  if (len > BODY_MAX - 3) return; // cannot happen with our callers
  body[0] = ftype;
  memcpy(body + 1, payload, len);
  uint16_t crc = crc16_ccitt(body, len + 1);
  body[len + 1] = (uint8_t)(crc & 0xFF); // little-endian
  body[len + 2] = (uint8_t)(crc >> 8);
  size_t n = cobs_encode(body, len + 3, enc);
  Serial.write(enc, n);
  Serial.write((uint8_t)0x00);
}

// ALL debug text leaves as LOG frames, never bare prints: a stray println
// would corrupt the COBS framing.
static void logf(const char *fmt, ...) {
  char buf[200];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  if (n < 0) return;
  if (n > (int)sizeof buf - 1) n = sizeof buf - 1;
  sendFrame(FTYPE_LOG, (const uint8_t *)buf, (size_t)n);
}

static void sendStatus() {
  BridgeStatus st = {};
  st.proto = 1;
  WiFi.macAddress(st.mac); // own STA MAC
  st.channel = gChannel;
  st.uptime_ms = millis();
  st.tx_ok = gTxOk;
  st.tx_fail = gTxFail;
  st.rx_pkts = gRxPkts;
  st.rx_drop = gRxDrop;
  st.crc_err = gCrcErr;
  strncpy(st.fw, CB_FW_STR, sizeof st.fw); // zero-pads the tail
  sendFrame(FTYPE_STATUS, (const uint8_t *)&st, sizeof st);
}

// ---- host -> bridge frame dispatch -----------------------------------------
static void handleFrame(uint8_t ftype, const uint8_t *payload, size_t len) {
  if (ftype == FTYPE_RADIO_TX) {
    if (len == 0 || len > ESPNOW_MAX) {
      logf("RADIO_TX len=%u rejected (1..%u)", (unsigned)len, (unsigned)ESPNOW_MAX);
      return;
    }
    // Send cb won't fire when the call itself errors, so count that here.
    if (esp_now_send(BCAST, payload, len) != ESP_OK) gTxFail = gTxFail + 1;
  } else if (ftype == FTYPE_CTRL && len >= 1) {
    switch (payload[0]) {
      case CTRL_STATUS_REQ:
        sendStatus();
        break;
      case CTRL_SET_CHANNEL:
        if (len >= 2 && payload[1] >= 1 && payload[1] <= 14 &&
            esp_wifi_set_channel(payload[1], WIFI_SECOND_CHAN_NONE) == ESP_OK) {
          gChannel = payload[1]; // RAM only: reboot returns to CB_CHANNEL
          logf("channel -> %u", gChannel);
        } else {
          logf("SET_CHANNEL rejected");
        }
        break;
      case CTRL_REBOOT:
        logf("rebooting");
        Serial.flush();
        ESP.restart();
        break;
      default:
        logf("unknown CTRL cmd 0x%02x", payload[0]);
    }
  } else {
    logf("unexpected ftype 0x%02x len=%u", ftype, (unsigned)len);
  }
}

// ---- serial RX: accumulate to 0x00, COBS-decode, CRC-check -----------------
static uint8_t gSerBuf[512]; // real frames encode to <= ~265 B
static size_t gSerLen = 0;

static void handleChunk(const uint8_t *chunk, size_t n) {
  uint8_t body[sizeof gSerBuf];
  size_t blen;
  if (cobs_decode(chunk, n, body, &blen) != 0 || blen < 3) {
    gCrcErr++; // COBS damage and CRC damage are the same operator problem
    return;
  }
  uint16_t crc = (uint16_t)body[blen - 2] | ((uint16_t)body[blen - 1] << 8);
  if (crc16_ccitt(body, blen - 2) != crc) {
    gCrcErr++;
    return; // drop silently; the counter is the report
  }
  handleFrame(body[0], body + 1, blen - 3);
}

static void pumpSerial() {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == 0x00) { // delimiter; bare zeros between frames are idle bytes
      if (gSerLen > 0) handleChunk(gSerBuf, gSerLen);
      gSerLen = 0;
    } else if (gSerLen < sizeof gSerBuf) {
      gSerBuf[gSerLen++] = (uint8_t)c;
    } else { // overlong garbage: drop and resync at the next delimiter
      gSerLen = 0;
      gCrcErr++;
    }
  }
}

// ---- lifecycle -------------------------------------------------------------
void setup() {
  Serial.begin(115200); // nominal; native USB CDC ignores the baud rate

  // ESP-NOW init per resonance-hardware espnow_link.cpp: STA mode but never
  // associated, explicit channel, single unencrypted broadcast peer (the
  // 150-node-scalable pattern -- encrypted peers cap at ~17). One deviation:
  // peer.channel = 0 ("follow the interface's current channel") so
  // CTRL_SET_CHANNEL only needs esp_wifi_set_channel, no peer re-add.
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(gChannel, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    logf("esp_now_init FAILED");
    return; // STATUS keeps flowing so the host can see the bridge is sick
  }
  esp_now_register_recv_cb(onEspNowRecv);
  esp_now_register_send_cb(onEspNowSend);
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = 0; // current channel (see note above)
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  sendStatus(); // HELLO
  logf("%s up, ch=%u", CB_FW_STR, gChannel);
}

void loop() {
  pumpSerial();

  // Drain the esp-now ring -> RADIO_RX frames [mac6][rssi:i8][raw packet].
  while (gTail != gHead) {
    RxSlot &s = gRing[gTail];
    uint8_t payload[RX_PAYLOAD_MAX];
    memcpy(payload, s.mac, 6);
    payload[6] = (uint8_t)s.rssi;
    memcpy(payload + 7, s.data, s.len);
    sendFrame(FTYPE_RADIO_RX, payload, 7 + (size_t)s.len);
    gTail = (gTail + 1) & 31;
  }

  static uint32_t lastStatus = 0;
  uint32_t now = millis();
  if (now - lastStatus >= 1000) {
    lastStatus = now;
    sendStatus();
  }
  delay(1);
}
