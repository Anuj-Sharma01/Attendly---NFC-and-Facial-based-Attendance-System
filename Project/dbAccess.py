import sqlite3
from datetime import datetime

def verifyTagValid(uid):
    with sqlite3.connect('main.db') as con:
        cursor = con.cursor()
        cursor.execute(f"SELECT * FROM tagInfo WHERE TAGID = '{uid}'")
        result = cursor.fetchall()
        if len(result) > 0:
            return True
        else:
            return False
        
def getStudentDetails(uid):
    with sqlite3.connect('main.db') as con:
        cursor = con.cursor()
        cursor.execute(f"SELECT * FROM studentInfo WHERE ROLLNO=(SELECT ROLLNO FROM tagInfo WHERE TAGID='{uid}');")
        return cursor.fetchall()

def updateAttendanceStatus(studentDetails):
    with sqlite3.connect('main.db') as con:
        cursor = con.cursor()
        cursor.execute(f"SELECT ROLLNO, DATE FROM attendanceInfo WHERE DATE LIKE '{datetime.now().date()}%' AND ROLLNO='{studentDetails[2]}'")
        result = cursor.fetchall()
        if len(result) == 0:
            cursor.execute(f"INSERT INTO attendanceInfo(ROLLNO, NAME, DATE, STATUS) values ('{studentDetails[2]}', '{studentDetails[1]}', '{datetime.now().replace(microsecond=0)}', 'Present');")