/*
 * ESP32 - FIRMWARE V9.6 (FIX NULL STATE)
 * - UI: Trạng thái Server to rõ, hiển thị luân phiên.
 * - Logic: Nếu Server trả về null/lỗi -> GIỮ NGUYÊN trạng thái cũ (Không nhảy về Absent).
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include "esp_camera.h"
#include <Wire.h>
#include "MAX30105.h"
#include "spo2_algorithm.h"
#include <U8g2lib.h>
#include <ArduinoJson.h>
#include "freertos/semphr.h"

// ==================== CẤU HÌNH CHÂN & THAM SỐ ====================
#define CAMERA_MODEL_ESP32S3_EYE
#include "camera_pins.h"

#define I2C_SDA_PIN 41
#define I2C_SCL_PIN 42
#define BUZZER_PIN 14
#define BUZZER_FREQUENCY 2000

#define IMAGE_QUALITY  30        
#define HTTPS_TIMEOUT 2000      
#define IMAGE_SEND_INTERVAL 500
#define WIFI_RECONNECT_INTERVAL 20000

#define I2C_SPEED_FAST 400000
#define BUFFER_SIZE 100

// ==================== BIẾN TOÀN CỤC ====================
volatile int serverStatusCode = 2; // Mặc định ban đầu là 2
volatile bool alertNoFingerSent = false;

// --- Biến Quản lý Trạng thái Đo ---
enum SensorState {
  STATE_IDLE,       // Chờ ngón tay
  STATE_MEASURING,  // Đang đếm ngược 30s
  STATE_FINISHED    // Đã đo xong, hiển thị kết quả
};

volatile SensorState currentSensorState = STATE_IDLE;
unsigned long measurementStartTime = 0;
const unsigned long MEASUREMENT_DURATION = 30000; // 30 giây
int finalHeartRate = 0;
float finalSpO2 = 0;

// --- Biến Hệ thống ---
unsigned long lastDebugTime = 0;
const unsigned long DEBUG_INTERVAL = 5000;
int frameCount = 0;
unsigned long totalHTTPSTime = 0;

// --- Biến Cảm biến ---
volatile int heartRate = 0;
volatile float spo2Value = 0.0;
volatile bool sensorDataReady = false;
volatile unsigned long lastValidReadingTime = 0;
volatile bool fingerDetected = false;

// --- Objects ---
MAX30105 particleSensor;
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, I2C_SCL_PIN, I2C_SDA_PIN);
SemaphoreHandle_t httpsMutex; 

// --- Biến SpO2 Algo ---
uint32_t irBuffer[BUFFER_SIZE];
uint32_t redBuffer[BUFFER_SIZE];
int32_t spo2;
int8_t validSPO2;
int32_t heartRateValue;
int8_t validHeartRate;

// --- WiFi Config ---
const char* ssid = "Hoang Phuc"; 
const char* password = "01082004"; 

const char* serverURL = "https://hphuc.esp32s3cam-pose-daktn5.id.vn/detect"; 
const char* sensorURL = "https://hphuc.esp32s3cam-pose-daktn5.id.vn/sensor_data";

// --- Buzzer & UI Vars ---
unsigned long buzzerStartTime = 0;
bool buzzerActive = false;
int beepPattern = 0;
unsigned long lastDisplayUpdate = 0;
const unsigned long DISPLAY_UPDATE_INTERVAL = 250;
unsigned long lastAlertTime = 0;
const unsigned long ALERT_COOLDOWN = 2000;
unsigned long lastWiFiReconnectAttempt = 0;
unsigned long lastImageSendTime = 0;

// ==================== CÁC HÀM HỖ TRỢ ====================

void calculateSPO2(uint32_t* irBuffer, uint32_t* redBuffer, int32_t& heartRateOut, int32_t& spo2Out, int8_t& validHROut, int8_t& validSpO2Out) {
    maxim_heart_rate_and_oxygen_saturation(irBuffer, BUFFER_SIZE, redBuffer, &spo2Out, &validSpO2Out, &heartRateOut, &validHROut);
}

void startBeep(int pattern) {
  if (millis() - lastAlertTime < ALERT_COOLDOWN) return;
  beepPattern = pattern;
  buzzerStartTime = millis();
  buzzerActive = true;
  lastAlertTime = millis();
  tone(BUZZER_PIN, BUZZER_FREQUENCY);
}

void updateBuzzer() {
  if (!buzzerActive) return;
  unsigned long elapsed = millis() - buzzerStartTime;
  switch(beepPattern) {
    case 1: if (elapsed >= 300) { noTone(BUZZER_PIN); buzzerActive = false; } break;
    case 2: 
      if (elapsed < 100) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else if (elapsed < 200) noTone(BUZZER_PIN);
      else if (elapsed < 300) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else if (elapsed < 400) noTone(BUZZER_PIN);
      else if (elapsed < 500) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else { noTone(BUZZER_PIN); buzzerActive = false; alertNoFingerSent = false; }
      break;
    case 3: 
      if (elapsed < 100) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else if (elapsed < 200) noTone(BUZZER_PIN);
      else if (elapsed < 300) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else { noTone(BUZZER_PIN); buzzerActive = false; }
      break;
    case 4: 
      if (elapsed < 1000) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else if (elapsed < 1200) noTone(BUZZER_PIN);
      else if (elapsed < 2200) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else { noTone(BUZZER_PIN); buzzerActive = false; } 
      break;
    case 5: 
      if (elapsed < 800) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else if (elapsed < 1000) noTone(BUZZER_PIN);
      else if (elapsed < 1800) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else if (elapsed < 2000) noTone(BUZZER_PIN);
      else if (elapsed < 2800) tone(BUZZER_PIN, BUZZER_FREQUENCY);
      else { noTone(BUZZER_PIN); buzzerActive = false; } 
      break;
  }
}

bool initDisplay() {
  u8g2.setI2CAddress(0x3C * 2); 
  if(!u8g2.begin()) return false;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_9x15B_tf); // Font to
  u8g2.drawStr(10, 30, "DAKT TEAM 5");
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(20, 50, "Khoi dong...");
  u8g2.sendBuffer();
  return true;
}

void displayStatus() {
  u8g2.clearBuffer();
  
  // === PHẦN 1: TRẠNG THÁI SERVER (HEADER) ===
  u8g2.setFont(u8g2_font_9x15B_tf); 
  
  switch(serverStatusCode) {
    case 0: u8g2.drawStr(25, 15, "CORRECT"); break;     
    case 1: u8g2.drawStr(15, 15, "INCORRECT"); break;   
    case 3: u8g2.drawStr(10, 15, "LONG WRONG!"); break; 
    case 4: u8g2.drawStr(15, 15, "LONG SIT!!"); break;  
    case 2: u8g2.drawStr(30, 15, "ABSENT"); break;      
    default: u8g2.drawStr(15, 15, "CONNECTING"); break;
  }
  
  u8g2.drawHLine(0, 20, 128);

  // === PHẦN 2: BODY (CẢM BIẾN & KẾT QUẢ) ===
  if (currentSensorState == STATE_MEASURING) {
      long remaining = (MEASUREMENT_DURATION - (millis() - measurementStartTime)) / 1000;
      if (remaining < 0) remaining = 0;

      u8g2.setFont(u8g2_font_6x12_tf); 
      u8g2.setCursor(0, 35); 
      u8g2.printf("DO: %02ds", (int)remaining);

      u8g2.setFont(u8g2_font_9x15B_tf);
      u8g2.setCursor(0, 55);
      if (heartRate > 0) {
          u8g2.printf("HR:%d", heartRate);
          u8g2.setCursor(75, 55);
          u8g2.printf("O2:%.0f", spo2Value);
      } else {
          u8g2.print("Dang do...");
      }

  } else if (currentSensorState == STATE_FINISHED) {
      unsigned long blinkState = (millis() / 2000) % 2;

      if (blinkState == 0) {
          u8g2.setFont(u8g2_font_6x12_tf);
          u8g2.setCursor(0, 35); u8g2.print("KET QUA CUOI:");
          u8g2.setFont(u8g2_font_9x15B_tf);
          u8g2.setCursor(5, 55); 
          u8g2.printf("HR:%d  O2:%.0f", finalHeartRate, finalSpO2);
      } else {
          u8g2.setFont(u8g2_font_9x15B_tf); 
          u8g2.setCursor(25, 45); 
          u8g2.print("DAT TAY"); 
          u8g2.setFont(u8g2_font_6x12_tf); 
          u8g2.setCursor(20, 60); 
          u8g2.print("(De do lai)");
      }

  } else {
      u8g2.setFont(u8g2_font_6x12_tf);
      u8g2.setCursor(0, 35); u8g2.print("San sang...");
      u8g2.setFont(u8g2_font_9x15B_tf);
      u8g2.setCursor(25, 55); 
      u8g2.print("DAT TAY");
  }

  u8g2.sendBuffer();
}

bool initMAX30102() {
  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) return false;
  
  byte ledBrightness = 0x1F; 
  byte sampleAverage = 4; 
  byte ledMode = 2;        
  int sampleRate = 100;    
  int pulseWidth = 411;    
  int adcRange = 4096;     
  
  particleSensor.setup(ledBrightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange);
  particleSensor.setPulseAmplitudeRed(ledBrightness);
  particleSensor.setPulseAmplitudeGreen(0);
  return true;
}

void connectWiFi() {
  Serial.printf("📡 Connecting to: %s\n", ssid);
  WiFi.disconnect(true);
  WiFi.setSleep(false);
  WiFi.begin(ssid, password);
  
  unsigned long startTime = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startTime < 15000) {
    delay(500); Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n✅ WiFi Connected\n");
  }
}

void maintainWiFiConnection() {
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastWiFiReconnectAttempt > WIFI_RECONNECT_INTERVAL) {
      connectWiFi();
      lastWiFiReconnectAttempt = millis();
    }
  }
}

esp_err_t initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  
  if(psramFound()){
    config.frame_size = FRAMESIZE_VGA; 
    config.jpeg_quality = IMAGE_QUALITY; 
    config.fb_count = 2;
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size = FRAMESIZE_SVGA;
    config.jpeg_quality = 12;
    config.fb_count = 1;
  }
  
  return esp_camera_init(&config);
}

void sendSensorData(int bpm, float spo2) {
  if (WiFi.status() != WL_CONNECTED) return;
  
  WiFiClientSecure client;
  client.setInsecure(); 
  client.setTimeout(HTTPS_TIMEOUT);
  HTTPClient https;
  https.setTimeout(HTTPS_TIMEOUT);
  https.setReuse(true); 
  
  if (https.begin(client, sensorURL)) {
    https.addHeader("Content-Type", "application/json");
    DynamicJsonDocument doc(128);
    doc["bpm"] = bpm;
    doc["spo2"] = spo2;
    String jsonData;
    serializeJson(doc, jsonData);
    int httpCode = https.POST(jsonData);
    if (httpCode > 0) {
      Serial.printf("✅ SENSOR SENT: HR=%d\n", bpm);
    } else {
      Serial.printf("❌ SENSOR ERR: %d\n", httpCode);
    }
    https.end();
  }
}

void captureAndSendImageOptimized() {
  if (WiFi.status() != WL_CONNECTED) return;
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) return;

  unsigned long start = millis();
  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(HTTPS_TIMEOUT);
  HTTPClient https;
  https.setTimeout(HTTPS_TIMEOUT);
  https.setReuse(true); 
  
  if (https.begin(client, serverURL)) {
    https.addHeader("Content-Type", "image/jpeg");
    int httpCode = https.POST(fb->buf, fb->len);
    if (httpCode > 0) {
      String payload = https.getString();
      if (payload.length() > 0 && payload.length() < 500) {
          DynamicJsonDocument doc(512);
          DeserializationError error = deserializeJson(doc, payload);
          
          // --- FIX QUAN TRỌNG: NẾU PARSE THÀNH CÔNG THÌ MỚI CẬP NHẬT ---
          if (!error) {
              int previousStatusCode = serverStatusCode;
              
              // Chỉ cập nhật nếu JSON có key "status_code"
              // Nếu null hoặc không có key -> GIỮ NGUYÊN GIÁ TRỊ CŨ
              if (doc.containsKey("status_code")) {
                  serverStatusCode = doc["status_code"]; 
              }
              
              // Logic còi báo động dựa trên trạng thái (mới hoặc cũ đều chạy tốt)
              if (serverStatusCode == 4 && previousStatusCode != 4) startBeep(5);
              else if (serverStatusCode == 3 && previousStatusCode != 3) startBeep(4);
              else if (serverStatusCode == 1 && previousStatusCode != 1) startBeep(1);
              else if (serverStatusCode == 0 || serverStatusCode == 2) {
                if (previousStatusCode == 1 || previousStatusCode == 3 || previousStatusCode == 4) {
                   noTone(BUZZER_PIN); buzzerActive = false;
                }
              }
              
              String msg = doc["message"].as<String>();
              unsigned long duration = millis() - start;
              totalHTTPSTime += duration;
              frameCount++;
              Serial.printf("📸 %dKB | %lums | %s\n", fb->len/1024, duration, msg.c_str());
          } else {
              Serial.println("⚠️ JSON Error or NULL -> Keep previous state");
          }
      }
    }
    https.end();
  }
  esp_camera_fb_return(fb);
}

void Task_CameraNetwork(void *pvParameters) {
  Serial.println("🚀 Task_CameraNetwork started");
  if (initCamera() != ESP_OK) vTaskDelete(NULL);
  
  while(1) {
    maintainWiFiConnection();
    if (WiFi.status() == WL_CONNECTED && (millis() - lastImageSendTime >= IMAGE_SEND_INTERVAL)) {
      if (!sensorDataReady) {
          if (xSemaphoreTake(httpsMutex, 200 / portTICK_PERIOD_MS) == pdTRUE) {
            captureAndSendImageOptimized();
            xSemaphoreGive(httpsMutex);
            lastImageSendTime = millis();
          }
      } else {
          // Serial.println("⏸️ Camera yielding..."); 
          vTaskDelay(500 / portTICK_PERIOD_MS);
      }
    }
    vTaskDelay(50 / portTICK_PERIOD_MS);
  }
}

void Task_Sensor(void *pvParameters) {
  Serial.println("🫀 Task_Sensor V9.5 Started");
  if (!initMAX30102()) vTaskDelete(NULL);

  const long IR_THRESHOLD = 50000; 
  int samplesRead = 0;
  unsigned long fingerMissingStartTime = 0;
  const unsigned long FINGER_REMOVE_DEBOUNCE = 1000; 

  while(1) {
    particleSensor.check(); 

    while (particleSensor.available()) {
      long irValue = particleSensor.getIR();
      long redValue = particleSensor.getRed();
      particleSensor.nextSample(); 

      if (irValue < IR_THRESHOLD) {
         if (fingerMissingStartTime == 0) fingerMissingStartTime = millis();
         if (millis() - fingerMissingStartTime > FINGER_REMOVE_DEBOUNCE) {
             fingerDetected = false;
             samplesRead = 0;
             sensorDataReady = false;
             if (currentSensorState == STATE_MEASURING) {
                 currentSensorState = STATE_IDLE;
                 Serial.println("🚫 Measurement Aborted");
             }
         }
      } else {
         fingerMissingStartTime = 0; 
         fingerDetected = true;
         
         if (currentSensorState == STATE_IDLE || currentSensorState == STATE_FINISHED) {
             currentSensorState = STATE_MEASURING;
             measurementStartTime = millis();
             samplesRead = 0;
             Serial.println("⏳ Starting 30s Measurement...");
             startBeep(3);
         }

         if (currentSensorState == STATE_MEASURING) {
             redBuffer[samplesRead] = redValue;
             irBuffer[samplesRead] = irValue;
             samplesRead++;

             if (millis() - measurementStartTime >= MEASUREMENT_DURATION) {
                 currentSensorState = STATE_FINISHED;
                 finalHeartRate = heartRate; 
                 finalSpO2 = spo2Value;
                 sensorDataReady = true; 
                 Serial.printf("✅ Finished! Result: HR=%d\n", finalHeartRate);
                 startBeep(3); 
             }

             if (samplesRead == BUFFER_SIZE) {
                maxim_heart_rate_and_oxygen_saturation(irBuffer, BUFFER_SIZE, redBuffer, &spo2, &validSPO2, &heartRateValue, &validHeartRate);
                if (validSPO2 == 1 && validHeartRate == 1) {
                   if (heartRateValue > 40 && heartRateValue < 220 && spo2 > 50 && spo2 <= 100) {
                       heartRate = heartRateValue;
                       spo2Value = spo2;
                       sensorDataReady = true; 
                   }
                }
                for (int i = 0; i < 75; i++) {
                  redBuffer[i] = redBuffer[i + 25];
                  irBuffer[i] = irBuffer[i + 25];
                }
                samplesRead = 75; 
             }
         }
      }
    }
    
    if (sensorDataReady && (currentSensorState == STATE_MEASURING || currentSensorState == STATE_FINISHED)) {
      if (millis() - lastImageSendTime > 2000) { 
          if (xSemaphoreTake(httpsMutex, 100 / portTICK_PERIOD_MS) == pdTRUE) {
            int sendHR = (currentSensorState == STATE_FINISHED) ? finalHeartRate : heartRate;
            float sendSpO2 = (currentSensorState == STATE_FINISHED) ? finalSpO2 : spo2Value;
            sendSensorData(sendHR, sendSpO2);
            xSemaphoreGive(httpsMutex);
            lastImageSendTime = millis();
            sensorDataReady = false; 
          }
      }
    }
    vTaskDelay(10 / portTICK_PERIOD_MS);
  }
}

void Task_Buzzer(void *pvParameters) {
  while(1) { updateBuzzer(); vTaskDelay(10 / portTICK_PERIOD_MS); }
}

void setup() {
  Serial.begin(115200);
  Serial.println("\n🚀 ESP32-S3 V9.6 - FIXED NULL");
  
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, 400000);
  pinMode(BUZZER_PIN, OUTPUT); noTone(BUZZER_PIN);
  
  httpsMutex = xSemaphoreCreateMutex();
  initDisplay();
  connectWiFi();

  xTaskCreatePinnedToCore(Task_CameraNetwork, "CamNet", 10240, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(Task_Sensor, "Sensor", 8192, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(Task_Buzzer, "Buzzer", 2048, NULL, 1, NULL, 0);

  startBeep(3);
  Serial.println("✅ SYSTEM STARTED");
}

void loop() {
  unsigned long now = millis();
  maintainWiFiConnection();
  if (now - lastDisplayUpdate >= DISPLAY_UPDATE_INTERVAL) {
    displayStatus();
    lastDisplayUpdate = now;
  }
  vTaskDelay(10 / portTICK_PERIOD_MS);
}