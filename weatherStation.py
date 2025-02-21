# Here is the Python code for the Raspberry Pi weather station script with all specified functionality.
# This script integrates the sensors (AHT20, Anemometer through ADS1115, SEN0562, and DHT11) and sends data to InfluxDB or the console.

from libcamera import Transform
from picamera2 import Picamera2
import time
from datetime import datetime
import requests
import argparse
from smbus2 import SMBus
import adafruit_ads1x15.ads1115 as ADC
from adafruit_ads1x15.analog_in import AnalogIn
from influxdb import InfluxDBClient
import psutil  # psutil will be used to capture system metrics like CPU and memory usage
import RPi.GPIO as GPIO
import board
import busio
import adafruit_ahtx0
import pigpio
import adafruit_bmp280
import os
import glob
import logging
import sys
import json 
import geopy.distance
from digitalio import DigitalInOut, Direction, Pull
from adafruit_pm25.i2c import PM25_I2C
import serial
from adafruit_pm25.uart import PM25_UART
from RPi_AS3935 import RPi_AS3935
import subprocess
from dotenv import load_dotenv

import logging
from logging.handlers import SysLogHandler

# Configure the root logger
logger = logging.getLogger("WeatherStation")
logger.setLevel(logging.INFO)  # Set the global logging level

# Create a SysLogHandler with a custom program name
syslog_handler = SysLogHandler(address='/dev/log')  # Or '/var/run/syslog' on macOS
formatter = logging.Formatter('%(name)s[%(process)d]: %(message)s')  # %(name)s now tied to "WeatherStation"
syslog_handler.setFormatter(formatter)

# Clear any existing handlers (to avoid disk logging)
logger.handlers.clear()
logger.addHandler(syslog_handler)

# Prevent propagation to other loggers
logger.propagate = False

class StreamToLogger:
    """Redirects stdout and stderr to the logger."""
    def __init__(self, log_level):
        self.log_level = log_level
        self.logger = logging.getLogger("WeatherStation")

    def write(self, message):
        if message.strip():  # Avoid blank lines
            self.logger.log(self.log_level, message)

    def flush(self):
        pass  # Required for file-like objects

# Redirect stdout and stderr
sys.stdout = StreamToLogger(logging.INFO)
sys.stderr = StreamToLogger(logging.ERROR)

print("Starting the script...")

# Load environment variables from .env file
load_dotenv()

# Configurable Parameters
AHT20_ADDRESS = 0x38
SEN0562_ADDRESS = 0x23
RELAY_PIN = 17              # GPIO pin for relay-controlled fan, changeable
UPDATE_FREQ = 30

# Fan Variables
FAN_MIN_TEMP = 80       # Temperature in F to start the fan at minimum speed
FAN_MAX_TEMP = 100      # Temperature in F to reach full fan speed
FAN_MIN_SPEED = 60      # Minimum fan speed percentage
FAN_MAX_SPEED = 100     # Maximum fan speed percentage
FAN_PWM_PIN = 12        # PWM pin connected to MOSFET controlling the fan

# Fetch STATION_LOCATION and convert it to a tuple of floats
station_location_str = os.environ.get("STATION_LOCATION", "")
if station_location_str:
    STATION_LOCATION = tuple(map(float, station_location_str.strip("()").split(",")))
else:
    STATION_LOCATION = None  # Handle missing value case

# Initialize pigpio and set up hardware PWM
pi = pigpio.pi()
if not pi.connected:
    raise Exception("Could not connect to pigpio daemon")
# Set PWM frequency to 25 kHz for quieter fan operation
pi.set_PWM_frequency(FAN_PWM_PIN, 40000)

# Track the fan's current state
fan_is_on = False  # Initial state of the fan

# Fetch InfluxDB credentials from environment variables
INFLUXDB_HOST = os.environ.get("INFLUXDB_HOST", "")
INFLUXDB_PORT = os.environ.get("INFLUXDB_PORT", "")
INFLUXDB_USER = os.environ.get("INFLUXDB_USER", "")
INFLUXDB_PASSWORD = os.environ.get("INFLUXDB_PASSWORD", "")
INFLUXDB_DB = os.environ.get("INFLUXDB_DB", "WeatherStation")
MEASUREMENT_NAME = os.environ.get("MEASUREMENT_NAME", "weather_data")

# Fetch Discord webhook URL from environment variables
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Initialize I2C bus and GPIO
bus = SMBus(1)
GPIO.setmode(GPIO.BCM)
GPIO.setup(RELAY_PIN, GPIO.OUT)

# Initialize the AS3935 (lightning) sensor
sensor = RPi_AS3935.RPi_AS3935(address=0x03, bus=1)
sensor.set_indoors(False)
sensor.set_noise_floor(0)
sensor.calibrate(tun_cap=0x0B)

# ADC configuration for anemometer (ADS1115)
# Initialize the I2C interface
i2c = busio.I2C(board.SCL, board.SDA)
# Create an ADS1115 object
adc = ADC.ADS1115(i2c)
bmp280 = adafruit_bmp280.Adafruit_BMP280_I2C(i2c)
# Define the analog input channel
anemometerChannel = AnalogIn(adc, ADC.P0)
WIND_SPEED_SCALE = 30 / 5  # Scale to convert 0-5V range to 0-30m/s

waterSensorChannel = AnalogIn(adc, ADC.P1)

# Optionally set the sea level pressure if altitude measurement is needed
bmp280.sea_level_pressure = 1028  # Chicago values

sensor = adafruit_ahtx0.AHTx0(board.I2C())

# Initialize the camera
picam2 = Picamera2()

transform = Transform(hflip=True, vflip=True)

# Configure camera for highest quality capture
config = picam2.create_still_configuration({"size": picam2.sensor_resolution},transform=transform)

picam2.configure(config)

# Set additional camera controls
picam2.set_controls({"AnalogueGain": 1.0, "AeEnable": False})

# Command-line arguments
parser = argparse.ArgumentParser(description="Raspberry Pi Weather Station")
parser.add_argument("--offline", action="store_true", help="Run in offline mode (disable InfluxDB and uptime check)")
parser.add_argument(
    "--fan-speed",
    type=int,
    choices=range(0, 101),
    metavar="[0-100]",
    help="Set the enclosure fan to a specific speed (0-100%)."
)
args = parser.parse_args()

# Initialize InfluxDB client with authentication (if not in offline mode)
client = None
if not args.offline:
    client = InfluxDBClient(
        host=INFLUXDB_HOST,
        port=INFLUXDB_PORT,
        username=INFLUXDB_USER,
        password=INFLUXDB_PASSWORD,
        database=INFLUXDB_DB,
        ssl=True,
        verify_ssl=True
    )

# PM2.5 Sensor Configuration
reset_pin = DigitalInOut(board.D24)
reset_pin.direction = Direction.OUTPUT
reset_pin.value = False
uart = serial.Serial("/dev/ttyS0", baudrate=9600, timeout=0.25)
pm25 = PM25_UART(uart, reset_pin)
    
# Function to initialize the AHT20 sensor
def read_aht20(bus):
    temperature = (sensor.temperature * 9 / 5) + 32
    humidity = sensor.relative_humidity
    return temperature, humidity

# Function to read the SEN0562 ambient light sensor
def read_sen0562(bus):
    # Send command to initiate measurement
    bus.write_byte(SEN0562_ADDRESS, 0x10)
    time.sleep(0.18)  # Wait at least 180ms as per the datasheet
    
    # Read two bytes of data
    data = bus.read_i2c_block_data(SEN0562_ADDRESS, 0x00, 2)
    
    # Combine the bytes into a single value
    lux = ((data[0] << 8) | data[1]) / 1.2
    return lux

# Function to read wind speed from anemometer (via ADS1115)
def read_anemometer():
    voltage = anemometerChannel.voltage
    wind_speed = voltage * WIND_SPEED_SCALE * 2.237  # Scale to m/s then to mph
    return wind_speed

# Function to read moisture sensor (via ADS1115)
def read_moisture():
    voltage = waterSensorChannel.voltage
    return voltage

# Function to read BMP280 metrics (temperature, pressure, and altitude)
def read_bmp280():
    temperature = bmp280.temperature * 9/5 + 32  # Convert Celsius to Fahrenheit
    pressure = bmp280.pressure  # Pressure in hPa
    altitude = bmp280.altitude * 3.281  # Altitude in feet
    return temperature, pressure, altitude

# Adjusted function for fan control with PWM
def control_fan(temp):
    # Determine fan speed based on temperature
    if temp < FAN_MIN_TEMP:
        speed = 0  # Turn fan off if below minimum threshold
    elif temp >= FAN_MAX_TEMP:
        speed = FAN_MAX_SPEED  # Full speed if at or above max threshold
    else:
        # Calculate the fan speed linearly between min and max thresholds
        speed = FAN_MIN_SPEED + (temp - FAN_MIN_TEMP) * (FAN_MAX_SPEED - FAN_MIN_SPEED) / (FAN_MAX_TEMP - FAN_MIN_TEMP)
    
    # Scale speed to PWM duty cycle (range 0 to 255 for pigpio)
    duty_cycle = int((speed / 100) * 255)  # Scale from percentage to 0-255
    pi.set_PWM_dutycycle(FAN_PWM_PIN, duty_cycle)
    return speed

# Function to report data to InfluxDB
def report_to_influxdb(data, measurement):
    json_body = [{
        "measurement": measurement,
        "fields": data
    }]
    if client:
        try:
            client.write_points(json_body)
            print(f"Data written to InfluxDB under '{measurement}' successfully.")
        except Exception as e:
            print(f"Failed to write data to InfluxDB ({measurement}): {e}")


def uptime_check(url):
    """
    Fetches the uptime URL with an appended RTT (in milliseconds) for pinging 192.168.2.1.

    Parameters:
        url (str): The base uptime monitoring URL to fetch.
    """
    try:
        # Ping 192.168.2.1 and calculate RTT
        ping_output = subprocess.run(
            ["ping", "-c", "1", "192.168.2.1"],
            capture_output=True,
            text=True
        )
        rtt = None
        if ping_output.returncode == 0:
            for line in ping_output.stdout.split("\n"):
                if "time=" in line:
                    rtt = float(line.split("time=")[1].split(" ")[0])  # Extract RTT
                    print(f"Ping RTT to 192.168.2.1: {rtt} ms")
                    break
        else:
            print("Ping failed. No RTT available.")

        # Append RTT as a query parameter to the URL
        if rtt is not None:
            full_url = f"{url}{rtt}"
        else:
            full_url = url

        # Send a GET request to the updated URL
        response = requests.get(full_url, timeout=5)
        print(f"Uptime check sent to {full_url}, Status: {response.status_code}")

    except Exception as e:
        print(f"Error in uptime_check: {e}")


# Function to get Raspberry Pi system metrics
def get_system_metrics():
    # CPU temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            cpu_temp = int(f.read()) / 1000.0  # Convert millidegrees to Celsius
    except FileNotFoundError:
        cpu_temp = None  # For systems that do not support this

    # Memory usage
    memory = psutil.virtual_memory()
    memory_usage = memory.percent

    # CPU load (percentage over all cores)
    cpu_load = psutil.cpu_percent(interval=None)

    return {
        "cpu_temperature": cpu_temp,
        "memory_usage": memory_usage,
        "cpu_load": cpu_load
    }

# Function to read PoE current (in amps) from the Pi
def read_poe_current():
    try:
        with open("/sys/devices/platform/soc/3f205000.i2c/i2c-11/i2c-0/0-0051/soc:i2c0mux:i2c@0:poe@51:rpi-poe-power-supply@f2/power_supply/rpi-poe/current_now", "r") as file:
            current_microamps = int(file.read().strip())
            current_amps = current_microamps / 1_000_000  # Convert microamps to amps
        return current_amps
    except FileNotFoundError:
        print("PoE current file not found. Ensure this Pi model supports PoE measurements.")
        return None
    except ValueError as e:
        print(f"Error reading PoE current: {e}")
        return None
    
# Function to read PoE fan speed from the Pi
def read_poe_fan():
    try:
        with open("/sys/devices/platform/pwm-fan/hwmon/hwmon1/pwm1", "r") as file:
            fan_speed_raw = int(file.read().strip())
            fan_speed = fan_speed_raw / 128  # Convert 0-128 to 0-1
        return fan_speed
    except FileNotFoundError:
        print("PoE fan file not found. Ensure the path is correct.")
        return None
    except ValueError as e:
        print(f"Error reading PoE fan speed: {e}")
        return None

# Cleanup function to stop PWM on exit
def cleanup():
    pi.set_PWM_dutycycle(FAN_PWM_PIN, 0)  # Stop PWM
    pi.stop()

# Updated function to capture wind speed continuously during each interval
def measure_wind_speed_interval(duration=UPDATE_FREQ, sample_interval=0.5):
    """Measures average and peak wind speed over a specified duration."""
    wind_speeds = []
    peak_speed = 0

    start_time = time.time()
    while time.time() - start_time < duration:
        # Measure current wind speed
        current_speed = read_anemometer()
        wind_speeds.append(current_speed)

        # Track peak speed
        if current_speed > peak_speed:
            peak_speed = current_speed

        # Wait before the next sample
        time.sleep(sample_interval)

    # Calculate average speed
    average_speed = sum(wind_speeds) / len(wind_speeds) if wind_speeds else 0
    return average_speed, peak_speed

# Adjusts the camera's exposure time based on lux level.
def set_exposure_from_lux(lux_value):
 
    # Map lux value to exposure time in microseconds
# Map lux value to exposure time in microseconds
    if lux_value < 50:
        exposure_time = int(25000 * (50 - lux_value) / 50) + 600000  # ~60ms down to ~5ms
    elif lux_value < 250:
        exposure_time = int(15000 * (250 - lux_value) / 200) + 10000  # ~25ms down to ~1ms
    elif lux_value < 1000:
        exposure_time = int(5000 * (1000 - lux_value) / 750) + 500  # ~10ms down to ~0.5ms
    else:
        exposure_time = 500  # Cap at 500 µs for very high light levels


    # Set exposure on the camera
    picam2.set_controls({"ExposureTime": exposure_time})
    print(f"Set exposure time to {exposure_time / 1_000_000:.2f} seconds for lux level {lux_value}")

def capture_and_upload_image():

    # Start the camera and capture image
    picam2.start()

    # Let the camera setup and focus
    time.sleep(2)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_path = f"/tmp/image_{timestamp}.jpg"
    
    # Capture image to file
    picam2.capture_file(image_path)
    picam2.stop()

    # Check if the image file was saved correctly
    if os.path.exists(image_path):
        #print(f"Image successfully captured and saved at {image_path}")
        pass
    else:
        print("Error: Image file not saved.")
        return

    # Prepare the file for upload with correct field name
    with open(image_path, "rb") as image_file:
        files = {'imageFile': (f"image_{timestamp}.jpg", image_file, 'image/jpeg')}
        url = os.environ.get("IMAGE_INTAKE_URL", "")

        # Send POST request with the image
        try:
            response = requests.post(url, files=files, timeout=5)
            #print(f"POST request sent to {url}")
        except requests.exceptions.RequestException as e:
            print(f"Error during POST request: {e}")
            return

    # Print response details
    print(f"Response Status Code: {response.status_code}")
    print(f"Response Text: {response.text}")

    # Check for successful upload
    if response.status_code == 200 and "Sorry" not in response.text:
        # print("Image uploaded successfully!")
        pass
    else:
        print("Failed to upload image. Please check the server response.")

    # Delete temporary file
    # print("Successfully deleted temporary files!")
    for f in glob.glob("/tmp/image_*.jpg"):
        os.remove(f)

def send_discord_notification(moisture_level, threshold=0.1):
    # Check if moisture level exceeds threshold
    if moisture_level > threshold:
        message = {
            "content": f":warning: Moisture Alert! Sensor reading is {moisture_level}, which exceeds the threshold of {threshold}."
        }
        
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=message)
            if response.status_code == 204:
                pass
            else:
                print(f"Failed to send notification. Status code: {response.status_code}, Response: {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"Error sending notification: {e}")

def calculate_wind_chill(temperature_f, wind_speed_mph):
    """
    Calculates the wind chill index using the North American formula (2001).

    Parameters:
        temperature_f (float): Temperature in degrees Fahrenheit.
        wind_speed_mph (float): Wind speed in miles per hour.

    Returns:
        float: Wind chill index in degrees Fahrenheit.
    """
    if wind_speed_mph < 3:
        # Wind chill is not significant for low wind speeds
        return temperature_f
    
    # Wind Chill formula
    wind_chill = (35.74 +
                  0.6215 * temperature_f -
                  35.75 * (wind_speed_mph ** 0.16) +
                  0.4275 * temperature_f * (wind_speed_mph ** 0.16))
    return round(wind_chill, 2)

def fetch_dump1090_stats(data_path="/run/dump1090-fa/aircraft.json"):
    """
    Fetches and processes statistics from dump1090-fa's JSON endpoint.

    Parameters:
        data_path (str): Path to the aircraft.json file.

    Returns:
        dict: A dictionary of relevant metrics for InfluxDB.
    """
    try:
        # Load the aircraft JSON data
        with open(data_path, "r") as f:
            aircraft_data = json.load(f)
        
        # Initialize metrics
        total_aircraft = 0
        max_altitude = 0
        max_speed = 0
        furthest_distance = 0
        closest_distance = float("inf")
        max_signal = -float("inf")  # RSSI is typically negative
        min_signal = float("inf")
        
        for flight in aircraft_data.get("aircraft", []):
            total_aircraft += 1  # Count each aircraft

            # Altitude
            if "alt_geom" in flight:
                max_altitude = max(max_altitude, flight["alt_geom"])
            
            # Ground Speed
            if "gs" in flight:
                max_speed = max(max_speed, flight["gs"])
            
            # Distance to station
            if "lat" in flight and "lon" in flight:
                distance = geopy.distance.geodesic(
                    (flight["lat"], flight["lon"]), STATION_LOCATION
                ).miles
                furthest_distance = max(furthest_distance, distance)
                closest_distance = min(closest_distance, distance)
            
            # Signal Strength
            if "rssi" in flight:
                max_signal = max(max_signal, flight["rssi"])
                min_signal = min(min_signal, flight["rssi"])

        # Return the collected metrics
        return {
            "total_aircraft": total_aircraft,
            "max_altitude": max_altitude,
            "max_speed": max_speed,
            "furthest_distance": furthest_distance,
            "closest_distance": closest_distance if closest_distance != float("inf") else 0,
            "max_signal": max_signal if max_signal != -float("inf") else 0,
            "min_signal": min_signal if min_signal != float("inf") else 0,
        }
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error fetching dump1090 stats: {e}")
        return None

def fetch_dump978_stats(data_path="/run/skyaware978/aircraft.json"):
    """
    Fetches and processes statistics from dump978-fa's JSON endpoint.

    Parameters:
        data_path (str): Path to the aircraft.json file.

    Returns:
        dict: A dictionary of relevant metrics for InfluxDB.
    """
    try:
        # Load the aircraft JSON data
        with open(data_path, "r") as f:
            aircraft_data = json.load(f)
        
        # Initialize metrics
        total_aircraft = 0
        max_altitude = 0
        max_speed = 0
        furthest_distance = 0
        closest_distance = float("inf")
        max_signal = -float("inf")  # RSSI is typically negative
        min_signal = float("inf")
        
        for flight in aircraft_data.get("aircraft", []):
            total_aircraft += 1  # Count each aircraft

            # Altitude
            if "alt_geom" in flight:
                max_altitude = max(max_altitude, flight["alt_geom"])
            
            # Ground Speed
            if "gs" in flight:
                max_speed = max(max_speed, flight["gs"])
            
            # Distance to station
            if "lat" in flight and "lon" in flight:
                distance = geopy.distance.geodesic(
                    (flight["lat"], flight["lon"]), STATION_LOCATION
                ).miles
                furthest_distance = max(furthest_distance, distance)
                closest_distance = min(closest_distance, distance)
            
            # Signal Strength
            if "rssi" in flight:
                max_signal = max(max_signal, flight["rssi"])
                min_signal = min(min_signal, flight["rssi"])

        # Return the collected metrics
        return {
            "total_aircraft": total_aircraft,
            "max_altitude": max_altitude,
            "max_speed": max_speed,
            "furthest_distance": furthest_distance,
            "closest_distance": closest_distance if closest_distance != float("inf") else 0,
            "max_signal": max_signal if max_signal != -float("inf") else 0,
            "min_signal": min_signal if min_signal != float("inf") else 0,
        }
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error fetching dump1090 stats: {e}")
        return None

def read_pm25_sensor(pm25):
    """
    Reads data from the PM2.5 air quality sensor and extracts key metrics.

    Parameters:
        pm25: The initialized PM2.5 sensor object.

    Returns:
        dict: A dictionary containing key metrics (or None if a read error occurs).
    """
    try:
        # Read data from the sensor
        aqdata = pm25.read()

        # Extract key metrics
        metrics = {
            "pm2_5": aqdata["pm25 standard"],       # PM2.5 concentration (µg/m³)
            "pm10": aqdata["pm100 standard"],       # PM10 concentration (µg/m³)
            "particles_0_3": aqdata["particles 03um"],  # Particle count >0.3 µm (per 0.1L)
            "particles_2_5": aqdata["particles 25um"],  # Particle count >2.5 µm (per 0.1L)
        }

        return metrics

    except RuntimeError:
        # Handle read errors gracefully
        print("Unable to read from PM2.5 sensor, retrying...")
        return None

# Interrupt handler for the AS3935 lightning sensor
def handle_lightning(channel):
    time.sleep(0.003)
    global sensor
    reason = sensor.get_interrupt()
    if reason == 0x01:
        print("Noise level too high - adjusting")
        sensor.raise_noise_floor()
    elif reason == 0x04:
        print("Disturber detected - masking")
        sensor.set_mask_disturber(True)
    elif reason == 0x08:
        now = datetime.now().strftime('%H:%M:%S - %Y/%m/%d')
        distance_km = sensor.get_distance()
        distance_miles = round(distance_km * 0.621371, 2)  # Convert km to miles

        # Print lightning strike details
        print("We sensed lightning!")
        print(f"It was {distance_miles} miles away. ({now})\n")

        message = {
            "content": f":zap: Lightning detected {distance_miles} miles away at {now}!"
        }

        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=message, timeout=5)
            if response.status_code == 204:
                print("Discord notification sent successfully.")
            else:
                print(f"Failed to send Discord notification: {response.status_code}, {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"Error sending Discord notification: {e}")

        # Record event to InfluxDB
        lightning_data = {
            "distance_miles": distance_miles,
            "timestamp": now
        }
        json_body = [{
            "measurement": "lightning_events",
            "fields": {
                "distance_miles": lightning_data["distance_miles"],
            },
            "time": lightning_data["timestamp"]
        }]

        if client:
            try:
                client.write_points(json_body)
                print("Lightning event recorded in InfluxDB.")
            except Exception as e:
                print(f"Failed to record lightning event to InfluxDB: {e}")

# Initialize the AS3935 sensor
lightningPin = 17
GPIO.setup(lightningPin, GPIO.IN)
GPIO.add_event_detect(lightningPin, GPIO.RISING, callback=handle_lightning)

# Main function to capture all metrics and include BMP280 readings
def main():
    while True:
        
        # Measure wind speed continuously over the 30-second interval
        avg_wind_speed, peak_wind_speed = measure_wind_speed_interval()

        # Read other sensors at the end of the interval
        temp_outdoor, humidity_outdoor = read_aht20(bus)
        lux = read_sen0562(bus)
        temp_indoor, pressure, altitude = read_bmp280()
        moisture = read_moisture()
        poe_current = read_poe_current()
        poe_fan_speed = read_poe_fan()
        wind_chill = calculate_wind_chill(temp_outdoor, avg_wind_speed)
        system_metrics = get_system_metrics()

        if args.fan_speed is not None:
            # Scale the percentage to a PWM duty cycle (0-255 for pigpio)
            duty_cycle = int((args.fan_speed / 100) * 255)
            pi.set_PWM_dutycycle(FAN_PWM_PIN, duty_cycle)
            fan_state = args.fan_speed
            print(f"Fan speed manually set to {args.fan_speed}%")
        else:
            # Control the fan based on temperature
            fan_state = control_fan(temp_indoor)

        # Dump1090 Metrics
        dump1090_stats = fetch_dump1090_stats()

        # Dump978 Metrics
        dump978_stats = fetch_dump978_stats()

        # Check moisture content
        send_discord_notification(moisture)

        # Prepare data dictionary with all metrics
        weather_data = {
            "temperature_outdoor": temp_outdoor,
            "humidity_outdoor": humidity_outdoor,
            "lux": lux,
            "avg_wind_speed": avg_wind_speed,  # Average wind speed over interval
            "peak_wind_speed": peak_wind_speed,  # Peak wind speed over interval
            "temperature_indoor": temp_indoor,
            "pressure": pressure,
            "altitude": altitude,
            "fan_state": fan_state,
            "moisture": moisture,
            "cpu_temperature": system_metrics["cpu_temperature"],
            "memory_usage": system_metrics["memory_usage"],
            "cpu_load": system_metrics["cpu_load"],
            "poe_current": poe_current,
            "poe_fan_speed": poe_fan_speed,
            "wind_chill": wind_chill
        }

        # Read PM2.5 data
        pm25_metrics = read_pm25_sensor(pm25)
        if pm25_metrics:
            weather_data.update(pm25_metrics)

        # Combine ADS-B data
        dump1090_data = {}
        if dump1090_stats:
            dump1090_data.update(dump1090_stats)

        dump978_data = {}
        if dump978_stats:
            dump978_data.update(dump978_stats)
        print(dump978_data)
        # Adjust exposure based on lux value
        set_exposure_from_lux(lux)

        capture_and_upload_image()

       # else:
        try:
 	    # Send Lux to Node-RED
            payload = {'lux': lux}
            r = requests.get('http://192.168.2.166:1880/outsidelux', params=payload, timeout=5)
            r.close()
        except requests.exceptions.RequestException as e:
            print(f"Node-RED request error: {e}")
        # Report data
        if args.offline:
            print("Weather Station Data:", weather_data)
            print("dump1090 Data:", dump1090_data)
            print("dump978 Data:", dump978_data)
        else:
            report_to_influxdb(weather_data, "weather_data")  # Send weather data
            if dump1090_data:
                report_to_influxdb(dump1090_data, "dump1090_data")  # Send ADS-B data
            if dump978_data:
                report_to_influxdb(dump978_data, "dump978_data") # Send UAT data

            # Perform uptime check
            uptime_url = os.environ.get("UPTIME_KUMA_URL", "")
            #print("Performing uptime check...")
            uptime_check(uptime_url)
            
        #time.sleep(UPDATE_FREQ)

try:
    main()
except KeyboardInterrupt:
    print("\nProgram interrupted by user.")
finally:
    cleanup()
    print("Cleanup complete.")
