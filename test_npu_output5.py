#!/usr/bin/env python3
import numpy as np
import cv2
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from npu_inference import NPUModel, INPUT_SIZE, _sigmoid

MODEL_PATH = '/home/orangepi/ai-agent/models/yolov5.nb'
TEST_IMAGE = '/opt/yolov5/input_data/dog_640_640.jpg'

YOLOV5_ANCHORS = [
    [(10, 13), (16, 30), (33, 23)],
    [(30, 61), (62, 45), (59, 119)],
    [(116, 90), (156, 198), (373, 326)],
]

COCO_STANDARD = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def _iou(a, b):
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0, a[2]-a[0]) * max(0, a[3]-a[1])
    area_b = max(0, b[2]-b[0]) * max(0, b[3]-b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


def nms(dets, thresh=0.45, max_det=50):
    if not dets:
        return []
    dets.sort(key=lambda d: d[1], reverse=True)
    dets = dets[:max_det * 3]
    keep = []
    while dets:
        best = dets.pop(0)
        keep.append(best)
        if len(keep) >= max_det:
            break
        dets = [d for d in dets if _iou(best[0], d[0]) < thresh]
    return keep


model = NPUModel(MODEL_PATH)
if not model.loaded:
    print('FAIL')
    exit(1)

img = cv2.imread(TEST_IMAGE)
img_r = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE))
img_rgb = cv2.cvtColor(img_r, cv2.COLOR_BGR2RGB)
input_bytes = img_rgb.astype(np.uint8).tobytes()
outputs = model.run(input_bytes)

strides = [8, 16, 32]

print("=== Test per-anchor chunk layout: (3, 85, grid, grid) ===")
print("Each anchor has its own 85 channels in sequence")

all_dets = []
for si in range(3):
    data = np.frombuffer(outputs[si], dtype=np.float32)
    grid = INPUT_SIZE // strides[si]
    stride = strides[si]
    total = 3 * 85 * grid * grid
    if data.size < total:
        print(f"  Scale {si}: insufficient data")
        continue

    reshaped = data.reshape(3, 85, grid, grid)
    gx, gy = np.meshgrid(np.arange(grid), np.arange(grid))

    for ai in range(3):
        aw, ah = YOLOV5_ANCHORS[si][ai]
        chunk = reshaped[ai]  # shape (85, grid, grid)

        obj = _sigmoid(chunk[0, :, :])
        tx = chunk[1, :, :]
        ty = chunk[2, :, :]
        tw = chunk[3, :, :]
        th = chunk[4, :, :]
        cls = _sigmoid(chunk[5:85, :, :].transpose(1, 2, 0))

        cx = (_sigmoid(tx) * 2 - 0.5 + gx) * stride
        cy = (_sigmoid(ty) * 2 - 0.5 + gy) * stride
        w = (_sigmoid(tw) * 2) ** 2 * aw
        h = (_sigmoid(th) * 2) ** 2 * ah
        max_cls = np.max(cls, axis=-1)
        conf = obj * max_cls

        mask = conf > 0.3
        indices = np.argwhere(mask)
        count = len(indices)
        if count > 0:
            print(f"  Scale {si} Anchor {ai}: {count} detections")
        for idx in indices:
            yi, xi = int(idx[0]), int(idx[1])
            bw = float(w[yi, xi])
            bh = float(h[yi, xi])
            if bw < 5 or bh < 5:
                continue
            bcx = float(cx[yi, xi])
            bcy = float(cy[yi, xi])
            bconf = float(conf[yi, xi])
            cid = int(np.argmax(cls[yi, xi]))
            all_dets.append(([bcx-bw/2, bcy-bh/2, bcx+bw/2, bcy+bh/2], bconf, cid))

nms_d = nms(all_dets)
print(f"\nTotal detections: {len(all_dets)}, after NMS: {len(nms_d)}")
for d in nms_d[:20]:
    box, conf, cid = d
    label = COCO_STANDARD[cid] if cid < len(COCO_STANDARD) else f"c{cid}"
    print(f"  [{cid:2d}] {label}: {conf:.4f} [{box[0]:.0f},{box[1]:.0f},{box[2]:.0f},{box[3]:.0f}]")

print("\n=== Now also try different channel mappings within per-anchor chunk ===")
for ch_map_name, ch_map_func in [
    ("standard: obj,tx,ty,tw,th,cls", lambda c, a: (_sigmoid(c[0]), c[1], c[2], c[3], c[4], _sigmoid(c[5:85].transpose(1,2,0)))),
    ("obj,ty,tx,th,tw,cls", lambda c, a: (_sigmoid(c[0]), c[2], c[1], c[4], c[3], _sigmoid(c[5:85].transpose(1,2,0)))),
    ("tx,ty,obj,tw,th,cls", lambda c, a: (_sigmoid(c[2]), c[0], c[1], c[3], c[4], _sigmoid(c[5:85].transpose(1,2,0)))),
    ("obj,tx,ty,th,tw,cls", lambda c, a: (_sigmoid(c[0]), c[1], c[2], c[4], c[3], _sigmoid(c[5:85].transpose(1,2,0)))),
]:
    test_dets = []
    for si in range(3):
        data = np.frombuffer(outputs[si], dtype=np.float32)
        grid = INPUT_SIZE // strides[si]
        stride = strides[si]
        total = 3 * 85 * grid * grid
        if data.size < total:
            continue
        reshaped = data.reshape(3, 85, grid, grid)
        gx, gy = np.meshgrid(np.arange(grid), np.arange(grid))
        for ai in range(3):
            aw, ah = YOLOV5_ANCHORS[si][ai]
            chunk = reshaped[ai]
            try:
                obj, tx, ty, tw, th, cls = ch_map_func(chunk, ai)
                cx = (_sigmoid(tx) * 2 - 0.5 + gx) * stride
                cy = (_sigmoid(ty) * 2 - 0.5 + gy) * stride
                w = (_sigmoid(tw) * 2) ** 2 * aw
                h = (_sigmoid(th) * 2) ** 2 * ah
                max_cls = np.max(cls, axis=-1)
                conf = obj * max_cls
                mask = (conf > 0.3) & (w > 5) & (h > 5)
                indices = np.argwhere(mask)
                for idx in indices:
                    yi, xi = int(idx[0]), int(idx[1])
                    bcx = float(cx[yi, xi])
                    bcy = float(cy[yi, xi])
                    bw = float(w[yi, xi])
                    bh = float(h[yi, xi])
                    bconf = float(conf[yi, xi])
                    cid = int(np.argmax(cls[yi, xi]))
                    test_dets.append(([bcx-bw/2, bcy-bh/2, bcx+bw/2, bcy+bh/2], bconf, cid))
            except:
                pass

    test_nms = nms(test_dets)
    print(f"  {ch_map_name}: {len(test_nms)} detections after NMS")
    for d in test_nms[:5]:
        box, conf, cid = d
        label = COCO_STANDARD[cid] if cid < len(COCO_STANDARD) else f"c{cid}"
        print(f"    [{cid:2d}] {label}: {conf:.4f} [{box[0]:.0f},{box[1]:.0f},{box[2]:.0f},{box[3]:.0f}]")
