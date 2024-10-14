import time
from datetime import datetime, timedelta
from threading import Thread
from bluepy.btle import Peripheral, DefaultDelegate, BTLEException, UUID
import firebase_admin
from firebase_admin import credentials, db, storage
from smbus2 import SMBus
import os
import cv2
import pytz
import schedule
import json
import socket
import subprocess
import signal
import paho.mqtt.client as mqtt

# Firebase setup
cred = credentials.Certificate('/home/arshsure/MykiOp/mykimate-c465e-firebase-adminsdk-zi2kx-20cd906068.json')
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://mykimate-c465e-default-rtdb.asia-southeast1.firebasedatabase.app/',
    'storageBucket': 'mykimate-c465e.appspot.com'
})

# Global variables
recording = False
video_path = ""
video_name = ""
bus_id = "630"
process = None
session_offender_count = 0
session_enter_count = 0
session_myki_taps = 0
recording_thread = None
local_data_cache = []
current_enter_count = 0
current_offender_count = 0
current_myki_taps = 0
network_available = True
previous_timestamp = None  # To track the previous timestamp

# I2C setup for the GJD 1602IIC LED display
I2C_ADDR = 0x3f  # Replace with your detected I2C address
bus = SMBus(1)

# LCD constants
LCD_CHR = 1  # Mode - Sending data
LCD_CMD = 0  # Mode - Sending command
LCD_LINE_1 = 0x80  # LCD RAM address for the 1st line
LCD_LINE_2 = 0xC0  # LCD RAM address for the 2nd line
LCD_BACKLIGHT = 0x08  # On
ENABLE = 0b00000100  # Enable bit
E_PULSE = 0.0005
E_DELAY = 0.0005

# Initialize LCD
def lcd_init():
    lcd_byte(0x33, LCD_CMD)  # Initialize
    lcd_byte(0x32, LCD_CMD)  # Initialize
    lcd_byte(0x06, LCD_CMD)  # Cursor move direction
    lcd_byte(0x0C, LCD_CMD)  # Turn cursor off
    lcd_byte(0x28, LCD_CMD)  # 2 line display
    lcd_byte(0x01, LCD_CMD)  # Clear display
    time.sleep(E_DELAY)

# Sending commands to LCD
def lcd_byte(bits, mode):
    bits_high = mode | (bits & 0xF0) | LCD_BACKLIGHT
    bits_low = mode | ((bits << 4) & ~ENABLE) | LCD_BACKLIGHT
    bus.write_byte(I2C_ADDR, bits_high)
    lcd_toggle_enable(bits_high)
    bus.write_byte(I2C_ADDR, bits_low)
    lcd_toggle_enable(bits_low)

def lcd_toggle_enable(bits):
    time.sleep(E_DELAY)
    bus.write_byte(I2C_ADDR, (bits | ENABLE))
    time.sleep(E_PULSE)
    bus.write_byte(I2C_ADDR, (bits & ~ENABLE))
    time.sleep(E_DELAY)

def lcd_string(message, line):
    message = message.ljust(16, " ")
    lcd_byte(line, LCD_CMD)
    for i in range(16):
        lcd_byte(ord(message[i]), LCD_CHR)

def update_display(enter_count, offender_count):
    lcd_string(f"Entered: {enter_count}", LCD_LINE_1)
    lcd_string(f"Offenders: {offender_count}", LCD_LINE_2)

def get_aest_timestamp():
    utc_now = datetime.utcnow()
    utc_now = pytz.utc.localize(utc_now)
    aest_now = utc_now.astimezone(pytz.timezone("Australia/Melbourne"))
    return aest_now.isoformat()

def fetch_firebase_data():
    global current_enter_count, current_offender_count, current_myki_taps, previous_timestamp
    if not is_network_available():
        print("Network unavailable. Skipping fetch from Firebase.")
        return
    ref = db.reference(f'bus_data/{bus_id}')
    data = ref.get()
    if data:
        current_enter_count = data.get('enter_count', 0)
        current_offender_count = data.get('offenders', 0)
        current_myki_taps = data.get('myki_taps', 0)
        last_updated = data.get('last_updated')
        print(f"Fetched data - Entered: {current_enter_count}, Offenders: {current_offender_count}, Last Updated: {last_updated}")
        
        if last_updated:
            previous_timestamp = datetime.fromisoformat(last_updated)
        else:
            previous_timestamp = datetime.now(pytz.timezone("Australia/Melbourne"))

        update_display(current_enter_count, current_offender_count)

# MQTT publish function
def mqtt_publish(topic, message):
    client = mqtt.Client()
    client.connect("localhost", 1883, 60)
    client.publish(topic, message)
    client.disconnect()

# Ensure data is fetched before publishing to MQTT
data = {
    'enter_count': current_enter_count,
    'offenders': current_offender_count,
    'myki_taps': current_myki_taps
}

# Publish to MQTT for the specified bus
mqtt_publish(f"bus_data/{bus_id}/enter_count", str(data['enter_count']))
mqtt_publish(f"bus_data/{bus_id}/offenders", str(data['offenders']))
mqtt_publish(f"bus_data/{bus_id}/myki_taps", str(data['myki_taps']))

# Update Firebase with session data and publish via MQTT
def update_firebase():
    global session_enter_count, session_myki_taps, session_offender_count, current_enter_count, current_offender_count, current_myki_taps
    if not is_network_available():
        print("Network unavailable. Saving data to local cache.")
        save_to_local_cache({
            'enter_count': session_enter_count,
            'myki_taps': session_myki_taps,
            'offenders': session_offender_count
        }, "firebase")
        session_reset()
        return

    ref = db.reference(f'bus_data/{bus_id}')
    current_data = ref.get()
    if current_data:
        current_enter_count = current_data.get('enter_count', 0)
        current_offender_count = current_data.get('offenders', 0)
        current_myki_taps = current_data.get('myki_taps', 0)

    data = {
        'last_updated': get_aest_timestamp(),
        'enter_count': current_enter_count + session_enter_count,
        'myki_taps': current_myki_taps + session_myki_taps,
        'offenders': current_offender_count + session_offender_count
    }
    
    try:
        ref.set(data)
        print(f"Data updated to Firebase - Entered: {data['enter_count']}, Offenders: {data['offenders']}")
        
        current_enter_count = data['enter_count']
        current_myki_taps = data['myki_taps']
        current_offender_count = data['offenders']
        update_display(current_enter_count, current_offender_count)
        
        # Publish the updated data to Node-RED via MQTT
        mqtt_publish(f"bus_data/{bus_id}/enter_count", str(data['enter_count']))
        mqtt_publish(f"bus_data/{bus_id}/offenders", str(data['offenders']))
        mqtt_publish(f"bus_data/{bus_id}/myki_taps", str(data['myki_taps']))
        
        session_reset()
        fetch_firebase_data()
    except Exception as e:
        print(f"Failed to update Firebase: {e}")
        save_to_local_cache(data, "firebase")

def upload_video_to_firebase(video_path, video_name):
    if not is_network_available():
        print("Network unavailable. Saving video path to local cache.")
        save_to_local_cache({"video_path": video_path, "video_name": video_name}, "video")
        return False
    bucket = storage.bucket()
    date_str = datetime.now().strftime("%Y-%m-%d")
    blob = bucket.blob(f'{bus_id}/{date_str}/{video_name}')
    try:
        blob.upload_from_filename(video_path)
        print(f"Video {video_name} uploaded to Firebase Storage.")
        return True
    except Exception as e:
        print(f"Failed to upload video {video_name}: {e}")
        save_to_local_cache({"video_path": video_path, "video_name": video_name}, "video")
        return False

def video_recording():
    global recording, video_path, video_name, process
    try:
        timestamp = get_aest_timestamp()
        video_name = f'video_{timestamp}.h264'
        video_path = os.path.join(os.getcwd(), f'{bus_id}', video_name)
        os.makedirs(os.path.join(os.getcwd(), f'{bus_id}'), exist_ok=True)

        command = [
            'libcamera-vid',
            '-t', '0',  # Record until stopped
            '-o', video_path,
            '--width', '640',
            '--height', '480',
            '--framerate', '30'
        ]

        process = subprocess.Popen(command)
        print(f"Started recording video. Saving to {video_path}")

    except Exception as e:
        print(f"Error during video recording: {e}")
        if process:
            process.terminate()
            process.wait()

def stop_video_recording():
    global recording, video_path, video_name, process
    if process:
        process.terminate()
        process.wait()
        process = None
        print(f"Video recording stopped. Video saved to: {video_path}")

        # Convert the video to MP4
        mp4_video_path = video_path.replace('.h264', '.mp4')
        command = ['ffmpeg', '-i', video_path, '-c', 'copy', mp4_video_path]
        convert_process = subprocess.run(command)

        if convert_process.returncode == 0:
            print(f"Video converted to MP4. Saved to: {mp4_video_path}")
            os.remove(video_path)  # Remove the original .h264 file
            video_path = mp4_video_path  # Update video_path to the MP4 file

        else:
            print(f"Failed to convert video to MP4. Conversion process exited with code {convert_process.returncode}")

def start_stop_video(recording_state):
    global recording, recording_thread
    if recording_state:
        if not recording:
            recording = True
            video_recording()
    else:
        if recording:
            recording = False
            stop_video_recording()

def process_video_upload():
    global video_path, video_name, session_offender_count
    if not video_path:
        print("No video path set. Skipping upload.")
        return
    if session_offender_count > 0:
        upload_successful = upload_video_to_firebase(video_path, video_name)
        if not upload_successful:
            print(f"Video upload failed. Path saved for retry: {video_path}")
    else:
        print("No offenders, video retained locally.")

class NotificationDelegate(DefaultDelegate):
    def __init__(self):
        DefaultDelegate.__init__(self)

    def handleNotification(self, cHandle, data):
        global session_offender_count, session_enter_count, session_myki_taps
        notification = data.decode('utf-8').strip()
        print(f"Notification received: {notification}")

        parts = notification.split(';')
        if parts[0] == "Door Open":
            print("Door opened.")
            start_stop_video(True)
            session_reset()  # Reset session data
        elif parts[0] == "Door Close":
            print("Door closed.")
            start_stop_video(False)
        elif len(parts) == 3 and parts[0].startswith("E:") and parts[1].startswith("T:") and parts[2].startswith("O:"):
            session_enter_count = int(parts[0].split(':')[1])
            session_myki_taps = int(parts[1].split(':')[1])
            session_offender_count = int(parts[2].split(':')[1])
            print(f"Session counts - Entered: {session_enter_count}, Myki Taps: {session_myki_taps}, Offenders: {session_offender_count}")
            process_video_upload()
            update_firebase()
        else:
            print(f"Unexpected notification format: {notification}")

def session_reset():
    global session_enter_count, session_myki_taps, session_offender_count
    session_enter_count = 0
    session_myki_taps = 0
    session_offender_count = 0

def connect_to_device(address):
    while True:
        try:
            print(f"Connecting to {address}")
            peripheral = Peripheral(address, "public")
            peripheral.setDelegate(NotificationDelegate())
            return peripheral
        except BTLEException as e:
            print(f"Connection failed: {e}. Retrying in 5 seconds...")
            time.sleep(5)

def save_to_local_cache(data, data_type):
    cache_file = "data_cache.json"
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                if os.path.getsize(cache_file) > 0:
                    local_data_cache = json.load(f)
                else:
                    local_data_cache = []
        else:
            local_data_cache = []

        local_data_cache.append({"data": data, "type": data_type, "timestamp": get_aest_timestamp()})

        with open(cache_file, 'w') as f:
            json.dump(local_data_cache, f)
        print(f"Saved to cache: {data}")
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading/writing cache file: {e}")
        # Repair the cache file by initializing an empty cache if corrupted
        with open(cache_file, 'w') as f:
            json.dump([], f)

def save_daily_data_if_new_day():
    global previous_timestamp
    current_timestamp = datetime.now(pytz.timezone("Australia/Melbourne"))
    if previous_timestamp and previous_timestamp.date() != current_timestamp.date():
        print("New day detected. Saving previous day's data.")
        ref = db.reference(f'bus_data/{bus_id}')
        data = ref.get()
        if data:
            # Save the data to a file
            date_str = previous_timestamp.strftime("%Y-%m-%d")
            filename = f'daily_data_{date_str}.json'
            daily_folder = os.path.join(os.getcwd(), f'{bus_id}', date_str)
            os.makedirs(daily_folder, exist_ok=True)
            file_path = os.path.join(daily_folder, filename)
            with open(file_path, 'w') as file:
                json.dump(data, file)

            # Upload the file to Firebase Storage
            bucket = storage.bucket()
            blob = bucket.blob(f'{bus_id}/{date_str}/{filename}')
            try:
                blob.upload_from_filename(file_path)
                print(f"Daily data file {filename} uploaded to Firebase Storage.")
            except Exception as e:
                print(f"Failed to upload daily data file {filename}: {e}")
                save_to_local_cache({"file_path": file_path, "filename": filename}, "daily_data")

            # Reset the counters in the database
            ref.update({
                'enter_count': 0,
                'myki_taps': 0,
                'offenders': 0,
                'last_updated': get_aest_timestamp()
            })
        previous_timestamp = current_timestamp

def retry_sending_cached_data():
    global network_available, current_enter_count, current_offender_count, current_myki_taps
    cache_file = "data_cache.json"
    try:
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
            with open(cache_file, 'r') as f:
                local_data_cache = json.load(f)
        else:
            local_data_cache = []

        new_cache = []
        for item in local_data_cache:
            if item["type"] == "firebase":
                if is_network_available():
                    # Fetch the current data from Firebase before updating
                    ref = db.reference(f'bus_data/{bus_id}')
                    current_data = ref.get()
                    if current_data:
                        current_enter_count = current_data.get('enter_count', 0)
                        current_offender_count = current_data.get('offenders', 0)
                        current_myki_taps = current_data.get('myki_taps', 0)

                    data = {
                        'last_updated': get_aest_timestamp(),
                        'enter_count': current_enter_count + item["data"]["enter_count"],
                        'myki_taps': current_myki_taps + item["data"]["myki_taps"],
                        'offenders': current_offender_count + item["data"]["offenders"]
                    }
                    ref.set(data)
                    print(f"Cached data updated to Firebase - Entered: {data['enter_count']}, Offenders: {data['offenders']}")
                    current_enter_count = data['enter_count']
                    current_myki_taps = data['myki_taps']
                    current_offender_count = data['offenders']
                    update_display(current_enter_count, current_offender_count)  # Update the LED display
                else:
                    new_cache.append(item)
            elif item["type"] == "video":
                if is_network_available():
                    upload_successful = upload_video_to_firebase(item["data"]["video_path"], item["data"]["video_name"])
                    if not upload_successful:
                        new_cache.append(item)
                    else:
                        print("Cached video uploaded to Firebase.")
                else:
                    new_cache.append(item)

        with open(cache_file, 'w') as f:
            json.dump(new_cache, f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading/writing cache file: {e}")
        # Repair the cache file by initializing an empty cache if corrupted
        with open(cache_file, 'w') as f:
            json.dump([], f)

def is_network_available():
    global network_available
    try:
        socket.create_connection(("www.google.com", 80))
        network_available = True
        return True
    except OSError:
        network_available = False
        return False

def monitor_network_status():
    global network_available
    while True:
        if not network_available and is_network_available():
            print("Network restored. Attempting to send cached data...")
            retry_sending_cached_data()
        time.sleep(5)

def main():
    global network_available
    lcd_init()
    lcd_string("Starting...", LCD_LINE_1)
    time.sleep(3)  # Display the starting message for 3 seconds

    fetch_firebase_data()  # Fetch and display initial data from Firebase

    network_monitor_thread = Thread(target=monitor_network_status)
    network_monitor_thread.daemon = True
    network_monitor_thread.start()

    arduino_address = "7C:9E:BD:68:78:C2"
    peripheral = connect_to_device(arduino_address)

    service_uuid = UUID("180F")
    char_uuid = UUID("2A19")
    service = peripheral.getServiceByUUID(service_uuid)
    char = service.getCharacteristics(char_uuid)[0]

    # Enable notifications
    peripheral.writeCharacteristic(char.getHandle() + 1, b"\x01\x00")
    print("Notifications enabled. Waiting for data...")

    try:
        while True:
            if peripheral.waitForNotifications(1.0):
                continue  # Handled in the delegate
            schedule.run_pending()
            save_daily_data_if_new_day()  # Check if a new day has started and save data if needed
    except BTLEException as e:
        print(f'Error during communication: {e}')
        peripheral.disconnect()
        main()
    except KeyboardInterrupt:
        peripheral.disconnect()
        print("Disconnected")

if __name__ == "__main__":
    main()