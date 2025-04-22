= 0:
        #     cursor.execute(f"INSERT INTO attendanceInfo(ROLLNO, NAME, DATE, STATUS) values ('{studentDetails[2]}', '{studentDetails[1]}', '{datetime.now().replace(microsecond=0)}', 'Present');")
        # else:
        #     for entry in result:
        #         print(studentDetails[2])
        #         print(entry[0])
        #         print(studentDetails[2] != entry[0])
        #         print(not str(datetime.now()).__contains__(str(entry[1]).split(' ')[0]))
        #         if studentDetails[2] != entry[0] and not str(datetime.now()).__contains__(str(entry[1]).split(' ')[0]):
        #             cursor.execute(f"INS