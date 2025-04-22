import serial
from dbAccess import *
from camAnal import captureAndCompare

ardPort = 'COM8'
baudRate = 9600

with serial.Serial(ardPort, baudRate, timeout = 2) as ser:
    lastID = None
    lastUID = None
    while True:
        currentUID = ser.readline().decode('utf-8').strip()
        if currentUID != lastUID:
            print(lastUID)
            lastUID = currentUID
            if verifyTagValid(lastUID):
                print("Camera on")
                face_names = captureAndCompare()
                studentDetails = getStudentDetails(lastUID)
                if face_names.__contains__(studentDetails[0][1]):
                    updateAttendanceStatus(studentDetails[0])
            else:
                print("Camera off")