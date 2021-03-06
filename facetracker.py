import copy
import os
import sys
import argparse

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("-i", "--ip", help="Set IP address for sending tracking data", default="127.0.0.1")
parser.add_argument("-p", "--port", type=int, help="Set port for sending tracking data", default=11573)
if os.name == 'nt':
    parser.add_argument("-l", "--list-cameras", type=int, help="Set this to 1 to list the available cameras and quit, set this to a number higher than 1 to output only the names", default=0)
    parser.add_argument("-W", "--width", type=int, help="Set camera and raw RGB width", default=640)
    parser.add_argument("-H", "--height", type=int, help="Set camera and raw RGB height", default=360)
    parser.add_argument("-F", "--fps", type=int, help="Set camera frames per second", default=24)
else:
    parser.add_argument("-W", "--width", type=int, help="Set raw RGB width", default=640)
    parser.add_argument("-H", "--height", type=int, help="Set raw RGB height", default=360)
parser.add_argument("-c", "--capture", help="Set camera ID (0, 1...) or video file", default="0")
parser.add_argument("-m", "--max-threads", type=int, help="Set the maximum number of threads", default=1)
parser.add_argument("-t", "--threshold", type=float, help="Set minimum confidence threshold for face detection", default=0.65)
parser.add_argument("-v", "--visualize", type=int, help="Set this to 1 to visualize the tracking, to 2 to also show face ids, to 3 to add confidence values or to 4 to add numbers to the point display", default=0)
parser.add_argument("-P", "--pnp-points", type=int, help="Set this to 1 to add the 3D fitting points to the visualization", default=0)
parser.add_argument("-s", "--silent", type=int, help="Set this to 1 to prevent text output on the console", default=0)
parser.add_argument("--faces", type=int, help="Set the maximum number of faces (slow)", default=1)
parser.add_argument("--scan-retinaface", type=int, help="When set to 1, scanning for additional faces will be performed using RetinaFace in a background thread, otherwise a simpler, faster face detection mechanism is used. When the maximum number of faces is 1, this option does nothing.", default=0)
parser.add_argument("--scan-every", type=int, help="Set after how many frames a scan for new faces should run", default=3)
parser.add_argument("--discard-after", type=int, help="Set the how long the tracker should keep looking for lost faces", default=10)
parser.add_argument("--max-feature-updates", type=int, help="This is the number of seconds after which feature min/max/medium values will no longer be updated once a face has been detected.", default=0)
parser.add_argument("--no-3d-adapt", type=int, help="When set to 1, the 3D face model will not be adapted to increase the fit", default=0)
parser.add_argument("--video-out", help="Set this to the filename of an AVI file to save the tracking visualization as a video", default=None)
parser.add_argument("--raw-rgb", type=int, help="When this is set, raw RGB frames of the size given with \"-W\" and \"-H\" are read from standard input instead of reading a video", default=0)
parser.add_argument("--log-data", help="You can set a filename to which tracking data will be logged here", default="")
parser.add_argument("--model", type=int, help="This can be used to select the tracking model. Higher numbers are models with better tracking quality, but slower speed. Models 1 and 0 tend to be too rigid for expression and blink detection.", default=3, choices=[0, 1, 2, 3])
parser.add_argument("--model-dir", help="This can be used to specify the path to the directory containing the .onnx model files", default=None)
parser.add_argument("--gaze-tracking", type=int, help="When set to 1, experimental blink detection and gaze tracking are enabled, which makes things slightly slower", default=1)
parser.add_argument("--face-id-offset", type=int, help="When set, this offset is added to all face ids, which can be useful for mixing tracking data from multiple network sources", default=0)
parser.add_argument("--repeat-video", type=int, help="When set to 1 and a video file was specified with -c, the tracker will loop the video until interrupted", default=0)
parser.add_argument("--dump-points", type=str, help="When set to a filename, the current face 3D points are made symmetric and dumped to the given file when quitting the visualization with the \"q\" key", default="")
if os.name == 'nt':
    parser.add_argument("--use-escapi", type=int, help="When set to 1, escapi will be used for video input instead of OpenCV", default=1)
args = parser.parse_args()

os.environ["OMP_NUM_THREADS"] = "1"

if os.name == 'nt' and args.list_cameras > 0:
    import escapi
    escapi.init()
    camera_count = escapi.count_capture_devices()
    if args.list_cameras == 1:
        print("Available cameras:")
    for i in range(camera_count):
        camera_name = escapi.device_name(i).decode()
        if args.list_cameras == 1:
            print(f"{i}: {camera_name}")
        else:
            print(camera_name)
    sys.exit(0)

import numpy as np
import time
import cv2
import socket
import struct
from input_reader import InputReader, VideoReader, list_cameras
from tracker import Tracker

target_ip = args.ip
target_port = args.port

if args.faces >= 40:
    print("Transmission of tracking data over network is not supported with 40 or more faces.")

fps = 0
if os.name == 'nt':
    fps = args.fps
    use_escapi_flag = True if args.use_escapi == 1 else False
    input_reader = InputReader(args.capture, args.raw_rgb, args.width, args.height, fps, use_escapi=use_escapi_flag)
else:
    input_reader = InputReader(args.capture, args.raw_rgb, args.width, args.height, fps, use_escapi=False)
if type(input_reader.reader) == VideoReader:
    fps = 0

log = None
out = None
first = True
height = 0
width = 0
tracker = None
sock = None
tracking_time = 0.0
tracking_frames = 0
frame_count = 0

features = ["eye_l", "eye_r", "eyebrow_steepness_l", "eyebrow_updown_l", "eyebrow_quirk_l", "eyebrow_steepness_r", "eyebrow_updown_r", "eyebrow_quirk_r", "mouth_corner_updown_l", "mouth_corner_inout_l", "mouth_corner_updown_r", "mouth_corner_inout_r", "mouth_open", "mouth_wide"]

if args.log_data != "":
    log = open(args.log_data, "w")
    log.write("Frame,Time,Width,Height,FPS,Face,FaceID,RightOpen,LeftOpen,AverageConfidence,Success3D,PnPError,RotationQuat.X,RotationQuat.Y,RotationQuat.Z,RotationQuat.W,Euler.X,Euler.Y,Euler.Z,RVec.X,RVec.Y,RVec.Z,TVec.X,TVec.Y,TVec.Z")
    for i in range(66):
        log.write(f",Landmark[{i}].X,Landmark[{i}].Y,Landmark[{i}].Confidence")
    for i in range(66):
        log.write(f",Point3D[{i}].X,Point3D[{i}].Y,Point3D[{i}].Z")
    for feature in features:
        log.write(f",{feature}")
    log.write("\r\n")
    log.flush()

try:
    frame_time = time.perf_counter()
    target_duration = 0
    if fps > 0:
        target_duration = 1. / float(fps)
    repeat = args.repeat_video != 0 and type(input_reader.reader) == VideoReader
    need_reinit = 0
    while repeat or input_reader.is_open():
        if not input_reader.is_open() or need_reinit == 1:
            input_reader = InputReader(args.capture, args.raw_rgb, args.width, args.height, fps)
            need_reinit = 2
            time.sleep(0.001)
            continue
        if not input_reader.is_ready():
            time.sleep(0.001)
            continue

        ret, frame = input_reader.read()
        if not ret:
            if repeat:
                if need_reinit == 0:
                    need_reinit = 1
                continue
            break

        need_reinit = 0
        frame_count += 1
        now = time.time()

        if first:
            first = False
            height, width, channels = frame.shape
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tracker = Tracker(width, height, threshold=args.threshold, max_threads=args.max_threads, max_faces=args.faces, discard_after=args.discard_after, scan_every=args.scan_every, silent=False if args.silent == 0 else True, model_type=args.model, model_dir=args.model_dir, no_gaze=False if args.gaze_tracking != 0 else True, use_retinaface=args.scan_retinaface, max_feature_updates=args.max_feature_updates, static_model=True if args.no_3d_adapt == 1 else False)
            if not args.video_out is None:
                out = cv2.VideoWriter(args.video_out, cv2.VideoWriter_fourcc('F','F','V','1'), 24, (width,height))

        inference_start = time.perf_counter()
        faces = tracker.predict(frame)
        if len(faces) > 0:
            tracking_time += (time.perf_counter() - inference_start) / len(faces)
            tracking_frames += 1
        packet = bytearray()
        detected = False
        for face_num, f in enumerate(faces):
            f = copy.copy(f)
            f.id += args.face_id_offset
            if f.eye_blink is None:
                f.eye_blink = [1, 1]
            right_state = "O" if f.eye_blink[0] > 0.30 else "-"
            left_state = "O" if f.eye_blink[1] > 0.30 else "-"
            if args.silent == 0:
                print(f"Confidence[{f.id}]: {f.conf:.4f} / 3D fitting error: {f.pnp_error:.4f} / Eyes: {left_state}, {right_state}")
            detected = True
            if not f.success:
                pts_3d = np.zeros((70, 3), np.float32)
            packet.extend(bytearray(struct.pack("d", now)))
            packet.extend(bytearray(struct.pack("i", f.id)))
            packet.extend(bytearray(struct.pack("f", width)))
            packet.extend(bytearray(struct.pack("f", height)))
            packet.extend(bytearray(struct.pack("f", f.eye_blink[0])))
            packet.extend(bytearray(struct.pack("f", f.eye_blink[1])))
            packet.extend(bytearray(struct.pack("B", 1 if f.success else 0)))
            packet.extend(bytearray(struct.pack("f", f.pnp_error)))
            packet.extend(bytearray(struct.pack("f", f.quaternion[0])))
            packet.extend(bytearray(struct.pack("f", f.quaternion[1])))
            packet.extend(bytearray(struct.pack("f", f.quaternion[2])))
            packet.extend(bytearray(struct.pack("f", f.quaternion[3])))
            packet.extend(bytearray(struct.pack("f", f.euler[0])))
            packet.extend(bytearray(struct.pack("f", f.euler[1])))
            packet.extend(bytearray(struct.pack("f", f.euler[2])))
            packet.extend(bytearray(struct.pack("f", f.translation[0])))
            packet.extend(bytearray(struct.pack("f", f.translation[1])))
            packet.extend(bytearray(struct.pack("f", f.translation[2])))
            if not log is None:
                log.write(f"{frame_count},{now},{width},{height},{args.fps},{face_num},{f.id},{f.eye_blink[0]},{f.eye_blink[1]},{f.conf},{f.success},{f.pnp_error},{f.quaternion[0]},{f.quaternion[1]},{f.quaternion[2]},{f.quaternion[3]},{f.euler[0]},{f.euler[1]},{f.euler[2]},{f.rotation[0]},{f.rotation[1]},{f.rotation[2]},{f.translation[0]},{f.translation[1]},{f.translation[2]}")
            for (x,y,c) in f.lms:
                packet.extend(bytearray(struct.pack("f", c)))
            if args.visualize > 1:
                frame = cv2.putText(frame, str(f.id), (int(f.bbox[0]), int(f.bbox[1])), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255,0,255))
            if args.visualize > 2:
                frame = cv2.putText(frame, f"{f.conf:.4f}", (int(f.bbox[0] + 18), int(f.bbox[1] - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0))
            for pt_num, (x,y,c) in enumerate(f.lms):
                packet.extend(bytearray(struct.pack("f", y)))
                packet.extend(bytearray(struct.pack("f", x)))
                if not log is None:
                    log.write(f",{y},{x},{c}")
                if pt_num == 66 and f.eye_blink[0] < 0.30:
                    continue
                if pt_num == 67 and f.eye_blink[1] < 0.30:
                    continue
                x = int(x + 0.5)
                y = int(y + 0.5)
                if args.visualize != 0 or not out is None:
                    if args.visualize > 3:
                        frame = cv2.putText(frame, str(pt_num), (int(y), int(x)), cv2.FONT_HERSHEY_SIMPLEX, 0.25, (255,255,0))
                    color = (0, 0, 255)
                    if pt_num >= 66:
                        color = (255, 255, 0)
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = color
                    x += 1
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = color
                    y += 1
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = color
                    x -= 1
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = color
            if args.pnp_points != 0 and (args.visualize != 0 or not out is None) and f.rotation is not None:
                if args.pnp_points > 1:
                    projected = cv2.projectPoints(f.face_3d[0:66], f.rotation, f.translation, tracker.camera, tracker.dist_coeffs)
                else:
                    projected = cv2.projectPoints(f.contour, f.rotation, f.translation, tracker.camera, tracker.dist_coeffs)
                for [(x,y)] in projected[0]:
                    x = int(x + 0.5)
                    y = int(y + 0.5)
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = (0, 255, 255)
                    x += 1
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = (0, 255, 255)
                    y += 1
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = (0, 255, 255)
                    x -= 1
                    if not (x < 0 or y < 0 or x >= height or y >= width):
                        frame[int(x), int(y)] = (0, 255, 255)
            for (x,y,z) in f.pts_3d:
                packet.extend(bytearray(struct.pack("f", x)))
                packet.extend(bytearray(struct.pack("f", -y)))
                packet.extend(bytearray(struct.pack("f", -z)))
                if not log is None:
                    log.write(f",{x},{-y},{-z}")
            if f.current_features is None:
                f.current_features = {}
            for feature in features:
                if not feature in f.current_features:
                    f.current_features[feature] = 0
                packet.extend(bytearray(struct.pack("f", f.current_features[feature])))
                if not log is None:
                    log.write(f",{f.current_features[feature]}")
            if not log is None:
                log.write("\r\n")
                log.flush()

        if detected and len(faces) < 40:
            sock.sendto(packet, (target_ip, target_port))

        if not out is None:
            out.write(frame)

        if args.visualize != 0:
            cv2.imshow('OpenSeeFace Visualization', frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                if args.dump_points != "" and not faces is None and len(faces) > 0:
                    np.set_printoptions(threshold=sys.maxsize, precision=15)
                    pairs = [
                        (0, 16),
                        (1, 15),
                        (2, 14),
                        (3, 13),
                        (4, 12),
                        (5, 11),
                        (6, 10),
                        (7, 9),
                        (17, 26),
                        (18, 25),
                        (19, 24),
                        (20, 23),
                        (21, 22),
                        (31, 35),
                        (32, 34),
                        (36, 45),
                        (37, 44),
                        (38, 43),
                        (39, 42),
                        (40, 47),
                        (41, 46),
                        (48, 52),
                        (49, 51),
                        (56, 54),
                        (57, 53),
                        (58, 62),
                        (59, 61),
                        (65, 63)
                    ]
                    points = copy.copy(faces[0].face_3d)
                    for a, b in pairs:
                        x = (points[a, 0] - points[b, 0]) / 2.0
                        y = (points[a, 1] + points[b, 1]) / 2.0
                        z = (points[a, 2] + points[b, 2]) / 2.0
                        points[a, 0] = x
                        points[b, 0] = -x
                        points[[a, b], 1] = y
                        points[[a, b], 2] = z
                    points[[8, 27, 28, 29, 33, 50, 55, 60, 64], 0] = 0.0
                    points[30, :] = 0.0
                    with open(args.dump_points, "w") as fh:
                        fh.write(repr(points))
                break

        duration = time.perf_counter() - frame_time
        while duration < target_duration:
            time.sleep(target_duration - duration)
            duration = time.perf_counter() - frame_time
        frame_time = time.perf_counter()
except KeyboardInterrupt:
    if args.silent == 0:
        print("Quitting")

input_reader.close()
if not out is None:
    out.release()
cv2.destroyAllWindows()

if args.silent == 0 and tracking_frames > 0:
    tracking_time = 1000 * tracking_time / tracking_frames
    print(f"Average tracking time per detected face: {tracking_time:.2f} ms")
