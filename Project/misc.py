import serial

# Set your Arduino serial port and baud rate
arduino_port = 'COM8'  # Change to your port
baud_rate = 9600

# Open the serial connection
with serial.Serial(arduino_port, baud_rate, timeout=2) as ser:
    last_uid = None  # Variable to track the last UID
    while True:
        # Read the line from the serial port
        current_uid = ser.readline().decode('utf-8').strip()

        # Only print the UID if it's different from the last one
        if current_uid and current_uid != last_uid:
            print(current_uid)
            last_uid = current_uid
