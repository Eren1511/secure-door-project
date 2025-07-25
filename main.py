import pigpio
import threading
import time
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor

from sensors.rfid_reader import RFIDReader
from actuators.servo_control import ServoController
from utils import gpio_pins
from sensors.pir_sensor import setup_pir_sensor
from sensors.ultrasonic_sensor import UltrasonicSensor
from sensors.magnetic_door_sensor import MagneticSensor
from pi_sender import send_status_async
from camera.mqtt_pub import send_image

import paho.mqtt.client as mqtt

PIR_PIN = gpio_pins.PIR_SENSOR_PIN
MAGNETIC_PIN = gpio_pins.MAGNETIC_DOOR_SENSOR_PIN

# Create a shared pigpio.pi() instance for all hardware
pi = pigpio.pi()
if not pi.connected:
    raise RuntimeError("Could not connect to pigpio daemon! Is pigpiod running?")

# Instantiate ultrasonic & magnetic sensors globally so they can be used in callbacks/threads
ultrasonic_sensor = UltrasonicSensor(pi=pi)
magnetic_sensor = MagneticSensor(pi=pi, pin=MAGNETIC_PIN)

# Globals and synchronization primitives
door_open_alarm_triggered = False
authorized_door_open = False
shutdown_event = threading.Event()
rfid_queue = Queue()

# --- IMPROVED: Better motion tracking with thread safety ---
motion_lock = threading.Lock()
last_motion_time = 0
motion_count = 0  # Debug counter
pir_callback_active = True

# --- Track time of last authorized door open and allowed open interval
last_authorized_open_time = 0
last_face_recognition_success = 0
AUTHORIZED_DOOR_OPEN_INTERVAL = 5  # seconds, adjust as needed
FACE_RECOGNITION_COOLDOWN = 10  # seconds after successful face recognition

# --- Face recognition state tracking ---
face_result_queue = Queue()
face_recognition_pending = False
face_recognition_start_time = 0
FACE_RECOGNITION_TIMEOUT = 5  # seconds

# --- Thread pool for async image operations ---
image_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ImageWorker")
active_image_futures = set()  # Track active image capture tasks

def on_face_result(client, userdata, msg):
    result = msg.payload.decode()
    print(f"[MQTT] Received face recognition result: {result}")
    face_result_queue.put(result)

def start_face_result_listener(broker_ip="192.168.166.195", topic="camera/result"):
    client = mqtt.Client()
    client.on_message = on_face_result
    client.connect(broker_ip, 1883, 60)
    client.subscribe(topic)
    client.loop_start()  # Run MQTT loop in background thread
    return client

def pir_motion_callback(gpio, level, tick):
    """Called by pigpio when PIR sensor goes high."""
    global last_motion_time, motion_count, pir_callback_active
    
    try:
        with motion_lock:
            current_time = time.time()
            last_motion_time = current_time
            motion_count += 1
            pir_callback_active = True
            print(f"[PIR] Motion detected! Count: {motion_count}, Time: {current_time:.2f}")
    except Exception as e:
        print(f"[PIR] Error in callback: {e}")

def pir_watchdog_thread(stop_event):
    """Monitor PIR sensor health and re-initialize if needed."""
    global pir_callback_active
    last_check_time = time.time()
    
    while not stop_event.is_set():
        current_time = time.time()
        
        # Check if PIR callback has been active in the last 30 seconds
        if (current_time - last_check_time) > 30:
            with motion_lock:
                if not pir_callback_active:
                    print("[PIR] Watchdog: PIR seems inactive, checking sensor...")
                    # Read PIR sensor state directly
                    pir_state = pi.read(PIR_PIN)
                    print(f"[PIR] Direct read: {pir_state}")
                
                # Reset the active flag for next check
                pir_callback_active = False
                last_check_time = current_time
        
        time.sleep(5)

def alarm_monitor_thread(stop_event):
    global door_open_alarm_triggered, authorized_door_open
    global last_authorized_open_time

    while not stop_event.is_set():
        try:
            door_state = magnetic_sensor.read()  # 1 = closed, 0 = open
            current_time = time.time()

            # If the door is open and NOT within the authorized open window, trigger the alarm
            if door_state == 0:
                # Check if the door was opened legally (recent authorized open)
                if not authorized_door_open:
                    # If not currently in authorized door open interval, trigger alarm
                    if (current_time - last_authorized_open_time) > AUTHORIZED_DOOR_OPEN_INTERVAL:
                        if not door_open_alarm_triggered:
                            print("ALARM: Door opened WITHOUT authorization!")
                            try:
                                send_status_async("PHYSICAL_ALARM")
                            except Exception as e:
                                print(f"Failed to send PHYSICAL_ALARM status: {e}")
                            door_open_alarm_triggered = True
                else:
                    # Door is open and authorized, do not trigger alarm
                    pass
            elif door_state == 1:
                # Door closed: reset the flags
                door_open_alarm_triggered = False
                authorized_door_open = False

        except Exception as e:
            print(f"[ALARM] Error in alarm monitor: {e}")

        # Sleep to avoid busy-waiting
        time.sleep(0.1)

def rfid_reader_thread(reader: RFIDReader, queue: Queue, stop_event: threading.Event):
    """
    Worker thread that continuously attempts to read an RFID card (with a short timeout).
    Whenever a UID is read (authorized or not), it pushes (authorized, uid) into the queue.
    """
    while not stop_event.is_set():
        try:
            # Attempt to read a card, waiting at most 0.5 seconds each call
            authorized, uid = reader.read_card()
            if uid:
                queue.put((authorized, uid))
        except Exception as e:
            print(f"[RFID] Error reading card: {e}")
        # Loop again immediately (reader.read_card will block up to timeout)

def process_rfid_events(reader, servo):
    """
    Process RFID events from the rfid_queue.
    """
    global authorized_door_open, door_open_alarm_triggered
    global last_authorized_open_time

    try:
        authorized, uid = rfid_queue.get_nowait()
    except Empty:
        return

    if uid:
        print(f"Card UID: {uid} - {'AUTHORIZED' if authorized else 'UNAUTHORIZED'}")

        if authorized:
            print("Access granted - Opening door.")
            try:
                send_status_async("AUTHORIZED_CARD")
            except Exception as e:
                print(f"Failed to send AUTHORIZED_CARD status: {e}")

            authorized_door_open = True
            last_authorized_open_time = time.time()  # Record time for legal open
            servo.set_angle(180)
            time.sleep(2)
            servo.set_angle(0)
            print("Please remove the card...")

            # Wait until the card is removed
            while True:
                try:
                    _, current_uid = reader.read_card()
                    if not current_uid:
                        break
                except Exception as e:
                    print(f"Error checking card removal: {e}")
                    break
                time.sleep(0.1)

        else:
            print("Access denied!")
            try:
                send_status_async("UNAUTHORIZED_CARD")
            except Exception as e:
                print(f"Failed to send UNAUTHORIZED_CARD status: {e}")

        # Small delay to avoid re-processing the same card
        time.sleep(0.5)

def check_motion_and_distance():
    """Check if motion + distance conditions are met for image capture."""
    with motion_lock:
        current_time = time.time()
        motion_recent = (current_time - last_motion_time) <= 5
        
    if motion_recent:
        try:
            distance = ultrasonic_sensor.get_distance()
            if distance is not None and distance <= 50:
                print(f"[IMAGE] Conditions met (distance={distance:.1f} cm, motion in last 5s)")
                return True
            else:
                print(f"[IMAGE] Distance too far: {distance} cm" if distance else "[IMAGE] Failed to read distance")
        except Exception as e:
            print(f"[IMAGE] Error reading ultrasonic: {e}")
    
    return False

def async_send_image():
    """Async wrapper for send_image - runs in thread pool."""
    try:
        print("[FACE] Starting async image capture...")
        send_image(use_test_image=False)
        print("[FACE] Image sent successfully")
        return True
    except Exception as e:
        print(f"[FACE] Failed to send image: {e}")
        return False

def start_face_recognition():
    """Start face recognition process (completely non-blocking)."""
    global face_recognition_pending, face_recognition_start_time, active_image_futures
    
    # Clean up any completed futures
    active_image_futures = {f for f in active_image_futures if not f.done()}
    
    # Limit concurrent image operations to prevent resource exhaustion
    if len(active_image_futures) >= 2:
        print("[FACE] Too many concurrent image operations, skipping...")
        return False
    
    try:
        # Submit image capture to thread pool
        future = image_executor.submit(async_send_image)
        active_image_futures.add(future)
        
        face_recognition_pending = True
        face_recognition_start_time = time.time()
        print("[FACE] Image capture started asynchronously...")
        return True
    except Exception as e:
        print(f"[FACE] Failed to start async image capture: {e}")
        return False

def check_face_recognition_result(servo):
    """Check for face recognition result (non-blocking)."""
    global face_recognition_pending, authorized_door_open, last_authorized_open_time, last_face_recognition_success, active_image_futures
    
    if not face_recognition_pending:
        return False
    
    current_time = time.time()
    
    # Clean up any completed image futures
    completed_futures = {f for f in active_image_futures if f.done()}
    for future in completed_futures:
        try:
            result = future.result()  # Get result and handle any exceptions
            if not result:
                print("[FACE] Image capture failed")
        except Exception as e:
            print(f"[FACE] Image capture exception: {e}")
    active_image_futures -= completed_futures
    
    # Check for timeout
    if (current_time - face_recognition_start_time) > FACE_RECOGNITION_TIMEOUT:
        print("[FACE] Face recognition timed out.")
        face_recognition_pending = False
        return False
    
    # Check for result
    try:
        result = face_result_queue.get_nowait()
        face_recognition_pending = False
        
        if result != "unknown":
            # Face recognized - open door
            print(f"[FACE] Face recognized: {result}! Activating servo.")
            authorized_door_open = True
            last_authorized_open_time = time.time()  # Record time for legal open
            last_face_recognition_success = time.time()  # Record successful face recognition
            try:
                send_status_async("AUTHORIZED_CARD")
            except Exception as e:
                print(f"Failed to send AUTHORIZED_CARD status: {e}")
            servo.set_angle(180)
            time.sleep(2)
            servo.set_angle(0)
            return True
        else:
            print("[FACE] No face match detected (unknown).")
            return False
            
    except Empty:
        # No result yet, keep waiting
        return False

def main():
    global authorized_door_open, last_motion_time, last_authorized_open_time, last_face_recognition_success

    # 1) Instantiate hardware interfaces
    authorized_uids = ["0C00201B99", "0C00203733"]
    reader = RFIDReader(serial_port="/dev/serial0", authorized_uids=authorized_uids)
    servo = ServoController(pin=gpio_pins.SERVO_PIN)

    # 2) Set up PIR sensor callback
    setup_pir_sensor(pi, PIR_PIN)
    pir_callback = pi.callback(PIR_PIN, pigpio.RISING_EDGE, pir_motion_callback)

    # 3) Start RFID reader thread
    rfid_thread = threading.Thread(
        target=rfid_reader_thread,
        args=(reader, rfid_queue, shutdown_event),
        daemon=True
    )
    rfid_thread.start()

    # 4) Start alarm monitor thread
    alarm_thread = threading.Thread(
        target=alarm_monitor_thread,
        args=(shutdown_event,),
        daemon=True
    )
    alarm_thread.start()

    # 5) Start PIR watchdog thread
    pir_watchdog = threading.Thread(
        target=pir_watchdog_thread,
        args=(shutdown_event,),
        daemon=True
    )
    pir_watchdog.start()

    # 6) Start listening for face recognition results
    face_result_mqtt_client = start_face_result_listener(broker_ip="192.168.166.195")

    print("System initialized. Waiting for activity...")
    
    # Track when we last attempted image capture to avoid spam
    last_image_attempt = 0
    IMAGE_COOLDOWN = 3  # seconds between image attempts

    try:
        while True:
            current_time = time.time()
            
            # --- Process RFID events (HIGHEST PRIORITY - always check first) ---
            process_rfid_events(reader, servo)
            
            # --- Check face recognition result (non-blocking) ---
            check_face_recognition_result(servo)

            # --- PIR + Ultrasonic "Send Image" logic ---
            # Only start new face recognition if not already pending
            if (not face_recognition_pending and 
                (current_time - last_image_attempt) > IMAGE_COOLDOWN and 
                (current_time - last_face_recognition_success) > FACE_RECOGNITION_COOLDOWN):
                
                if check_motion_and_distance():
                    last_image_attempt = current_time
                    start_face_recognition()

            # --- Periodic status output (every 30 seconds) ---
            if int(current_time) % 30 == 0:
                with motion_lock:
                    time_since_motion = current_time - last_motion_time if last_motion_time > 0 else float('inf')
                print(f"[STATUS] Motion count: {motion_count}, Last motion: {time_since_motion:.1f}s ago, Face pending: {face_recognition_pending}, Active images: {len(active_image_futures)}")
                time.sleep(1)  # Avoid multiple prints in the same second

            # --- Very short sleep to keep loop responsive ---
            time.sleep(0.05)  # Reduced from 0.1 to 0.05 for better responsiveness

    except KeyboardInterrupt:
        print("\nShutting down gracefully...")

    finally:
        # Signal threads to stop
        shutdown_event.set()
        
        # Wait for threads to finish
        for thread in [rfid_thread, alarm_thread, pir_watchdog]:
            thread.join(timeout=1.0)

        # Cleanup hardware
        try:
            reader.cleanup()
            servo.cleanup()
            ultrasonic_sensor.cleanup()
            pir_callback.cancel()
            pi.stop()
        except Exception as e:
            print(f"Error during cleanup: {e}")

        # Cleanup MQTT face result listener
        try:
            face_result_mqtt_client.loop_stop()
            face_result_mqtt_client.disconnect()
        except Exception as e:
            print(f"Error disconnecting MQTT: {e}")

        # Cleanup thread pool
        try:
            print("Shutting down image thread pool...")
            image_executor.shutdown(wait=True, timeout=5.0)
        except Exception as e:
            print(f"Error shutting down thread pool: {e}")

if __name__ == "__main__":
    main()