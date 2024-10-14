#include <ArduinoBLE.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <SR04.h>

#define SS_PIN 10
#define RST_PIN 9

MFRC522 rfid(SS_PIN, RST_PIN);

// Define pins for ultrasonic sensors
#define TRIG_PIN1 3
#define ECHO_PIN1 4
#define TRIG_PIN2 5
#define ECHO_PIN2 6

// Define pins for LEDs
#define LED_PIN1 7
#define LED_PIN2 8

SR04 sensor1 = SR04(ECHO_PIN1, TRIG_PIN1);  // left sensor
SR04 sensor2 = SR04(ECHO_PIN2, TRIG_PIN2);  // right sensor

BLEService busService("180F");
BLEStringCharacteristic entryExitCharacteristic("2A19", BLERead | BLENotify, 20);

float max_distance = 10;  // movement sensing range (in cm)

int peopleEntered = 0;
int peopleExited = 0;
int rfidTaps = 0;
int offenders = 0;

bool doorOpen = false;  // Tracks the actual state of the door

void setup() {
  Serial.begin(9600);
  delay(1000);

  SPI.begin();
  rfid.PCD_Init();

  pinMode(TRIG_PIN1, OUTPUT);
  pinMode(ECHO_PIN1, INPUT);
  pinMode(TRIG_PIN2, OUTPUT);
  pinMode(ECHO_PIN2, INPUT);

  // Initialize LED pins
  pinMode(LED_PIN1, OUTPUT);
  pinMode(LED_PIN2, OUTPUT);

  if (!BLE.begin()) {
    Serial.println("Starting BLE failed!");
    while (1);
  }

  BLE.setLocalName("myki-Op");
  BLE.setAdvertisedService(busService);
  busService.addCharacteristic(entryExitCharacteristic);
  BLE.addService(busService);

  entryExitCharacteristic.writeValue("0");
  BLE.advertise();

  Serial.println("BLE device is now advertising...");
}

void loop() {
  BLEDevice central = BLE.central();
  if (central) {
    Serial.print("Connected to central: ");
    Serial.println(central.address());

    while (central.connected()) {
      if (Serial.available() > 0) {
        String command = Serial.readStringUntil('\n');
        command.trim();
        if (command == "Door Open" && !doorOpen) {
          doorOpen = true;
          peopleEntered = 0;
          peopleExited = 0;
          rfidTaps = 0;
          offenders = 0;
          Serial.println("Door opened, starting detection.");
          entryExitCharacteristic.writeValue("Door Open");
        } else if (command == "Door Close" && doorOpen) {
          doorOpen = false;
          Serial.println("Door closed, stopping detection.");
          entryExitCharacteristic.writeValue("Door Close");
          calculateOffenders();
          sendBLEData();
        }
      }

      if (doorOpen) {
        checkSensors();
        checkRFID();
        delay(500); // Small delay for debounce
      }
    }
    Serial.print("Disconnected from central: ");
    Serial.println(central.address());
  }
}

void checkSensors() {
  long d1 = sensor1.Distance();
  long d2 = sensor2.Distance();
  
  if (d1 < max_distance && d2 >= max_distance) {
    Serial.println("Person entering");
    peopleEntered++;
    digitalWrite(LED_PIN1, HIGH); // Turn on LED for sensor 1
    delay(1500);
    digitalWrite(LED_PIN1, LOW); // Turn off LED for sensor 1
  } else if (d2 < max_distance && d1 >= max_distance) {
    Serial.println("Person exiting");
    peopleExited++;
    digitalWrite(LED_PIN2, HIGH); // Turn on LED for sensor 2
    delay(1500);
    digitalWrite(LED_PIN2, LOW); // Turn off LED for sensor 2
  }
}

void checkRFID() {
  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    Serial.print("UID tag: ");
    for (byte i = 0; i < rfid.uid.size; i++) {
      Serial.print(rfid.uid.uidByte[i] < 0x10 ? " 0" : " ");
      Serial.print(rfid.uid.uidByte[i], HEX);
    }
    Serial.println();
    rfidTaps++;
  }
}

void calculateOffenders() {
  offenders = peopleEntered - rfidTaps;
  if (offenders < 0) offenders = 0; // Ensure no negative offenders count
}

void sendBLEData() {
  // Construct a message with entry, exit, RFID tap, and offender counts
  String data = "E:" + String(peopleEntered) + ";T:" + String(rfidTaps) + ";O:" + String(offenders);
  Serial.println("Sending data: " + data);
  entryExitCharacteristic.writeValue(data);
}
