import face_recognition as fr
import cv2
import numpy as np
import os
from os import path

def captureAndCompare():
    Cap = cv2.VideoCapture(0)

    photosLocation = path.join(os.getcwd(), 'Photos')
    known_encodes = []
    students = []
    for image in os.listdir(photosLocation):
        imageFile = fr.load_image_file(path.join(photosLocation, image))
        known_encodes.append(fr.face_encodings(imageFile)[0])
        students.append(image.split('.')[0])

    #loading image from file and extracting the image encoding
    # as_image = fr.load_image_file(path.join(photosLocation, 'Anuj Sharma.jpg'))
    # as_encode = fr.face_encodings(as_image)[0]

    # ad_image = fr.load_image_file("D:\Anuj\Project\Photos\Anurag Deshmukh.png")
    # ad_encode = fr.face_encodings(ad_image)[0]

    # dk_image = fr.load_image_file("D:\Anuj\Project\Photos\Dhananjay Koshti.jpg")
    # dk_encode = fr.face_encodings(dk_image)[0]

    # rm_image = fr.load_image_file("D:\Anuj\Project\Photos\Rehan Mulla.jpg")
    # rm_encode = fr.face_encodings(rm_image)[0]

    # rp_image = fr.load_image_file("D:\Anuj\Project\Photos\Riddhi Patil.jpg")
    # rp_encode = fr.face_encodings(rp_image)[0]

    # rg_image = fr.load_image_file("D:\Anuj\Project\Photos\Ruchira Gangurde.jpg")
    # rg_encode = fr.face_encodings(rg_image)[0]

    # known_encodes = [as_encode,ad_encode,dk_encode,rm_encode,rp_encode,rg_encode]
    # students= ["Anuj Sharma","Anurag Deshmukh","Dhananjay Koshti","Rehan Mulla","Riddhi Patil","Ruchira Gangurde"]

    face_location = []
    face_encodings = []
    face_names = []

    face_names = []

    _, image = Cap.read()
    small_frame = cv2.resize(image ,(0,0), fx=0.25, fy=0.25)
    rgb_small_frame = small_frame[:,:,::-1]
    rgb_small_frame = cv2.cvtColor(rgb_small_frame , cv2.COLOR_BGR2RGB)

    face_location = fr.face_locations(rgb_small_frame)
    face_encodings = fr.face_encodings(rgb_small_frame,face_location)
    for face_encoding in face_encodings:
        matches = fr.compare_faces(known_encodes,face_encoding)
        name = ''
        face_distance = fr.face_distance(known_encodes, face_encoding)
        best_match_index = np.argmin(face_distance)
        if matches[best_match_index]:
            name = students[best_match_index]
        face_names.append(name)

    while True:
        cv2.imshow('attendance', image)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    Cap.release()
    cv2.destroyAllWindows()

    return face_names