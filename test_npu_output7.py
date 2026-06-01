#!/usr/bin/env python3
import numpy as np
import cv2
import sys
import os
from itertools import permutations

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
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    area_a = max(0, a[2]-a[0]) * max(0, a[3]-a[1])
    area_b = max(0, b[2]-b[0]) * max(0, b[3]-b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0

def nms(dets, thresh=0.45, max_det=30):
    if not dets: return []
    dets.sort(key=lambda d: d[1], reverse=True)
    dets = dets[:max_det*3]
    keep = []
    while dets:
        best = dets.pop(0)
        keep.append(best)
        if len(keep) >= max_det: break
        dets = [d for d in dets if _iou(best[0], d[0]) < thresh]
    return keep


model = NPUModel(MODEL_PATH)
img = cv2.imread(TEST_IMAGE)
img_r = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE))
img_rgb = cv2.cvtColor(img_r, cv2.COLOR_BGR2RGB)
input_bytes = img_rgb.astype(np.uint8).tobytes()
outputs = model.run(input_bytes)
strides = [8, 16, 32]

print("=== What if this is YOLOv5 but the 5 header channels are NOT at the beginning? ===")
print("=== What if the model outputs [80 classes, obj, x, y, w, h]? ===")

for cls_pos in [0, 80]:
    if cls_pos == 0:
        order_desc = "cls(0-79), obj(80), tx(81), ty(82), tw(83), th(84)"
    else:
        order_desc = "tx(0), ty(1), tw(2), th(3), obj(4), cls(5-84)"

    all_dets = []
    for si in range(3):
        data = np.frombuffer(outputs[si], dtype=np.float32)
        grid = INPUT_SIZE // strides[si]
        stride = strides[si]
        total = 85 * grid * grid * 3
        if data.size < total: continue
        reshaped = data.reshape(85, grid, grid, 3)
        gx, gy = np.meshgrid(np.arange(grid), np.arange(grid))

        for ai in range(3):
            aw, ah = YOLOV5_ANCHORS[si][ai]
            if cls_pos == 0:
                obj = _sigmoid(reshaped[80, :, :, ai])
                tx = reshaped[81, :, :, ai]
                ty = reshaped[82, :, :, ai]
                tw = reshaped[83, :, :, ai]
                th = reshaped[84, :, :, ai]
                cls = _sigmoid(reshaped[0:80, :, :, ai].transpose(1,2,0))
            else:
                obj = _sigmoid(reshaped[4, :, :, ai])
                tx = reshaped[0, :, :, ai]
                ty = reshaped[1, :, :, ai]
                tw = reshaped[2, :, :, ai]
                th = reshaped[3, :, :, ai]
                cls = _sigmoid(reshaped[5:85, :, :, ai].transpose(1,2,0))

            cx = (_sigmoid(tx) * 2 - 0.5 + gx) * stride
            cy = (_sigmoid(ty) * 2 - 0.5 + gy) * stride
            w = (_sigmoid(tw) * 2) ** 2 * aw
            h = (_sigmoid(th) * 2) ** 2 * ah
            max_cls = np.max(cls, axis=-1)
            conf = obj * max_cls

            mask = (conf > 0.2) & (w > 5) & (h > 5)
            indices = np.argwhere(mask)
            for idx in indices:
                yi, xi = int(idx[0]), int(idx[1])
                bw, bh = float(w[yi, xi]), float(h[yi, xi])
                bcx, bcy = float(cx[yi, xi]), float(cy[yi, xi])
                bconf = float(conf[yi, xi])
                cid = int(np.argmax(cls[yi, xi]))
                all_dets.append(([bcx-bw/2, bcy-bh/2, bcx+bw/2, bcy+bh/2], bconf, cid))

    nms_d = nms(all_dets)
    print(f"\n{order_desc}: {len(nms_d)} detections")
    for d in nms_d[:10]:
        box, conf, cid = d
        label = COCO_STANDARD[cid] if cid < len(COCO_STANDARD) else f"c{cid}"
        print(f"  [{cid:2d}] {label}: {conf:.4f} [{box[0]:.0f},{box[1]:.0f},{box[2]:.0f},{box[3]:.0f}]")

print("\n\n=== CRITICAL: Check if model output is actually INT8/quantized despite reporting FP32 ===")
for si in range(3):
    data = np.frombuffer(outputs[si], dtype=np.float32)
    data_as_int8 = np.frombuffer(outputs[si], dtype=np.int8)
    print(f"\nScale {si}:")
    print(f"  As FP32: min={data.min():.4f} max={data.max():.4f} mean={data.mean():.4f}")
    print(f"  As INT8: min={data_as_int8.min()} max={data_as_int8.max()} mean={data_as_int8.mean():.2f}")
    print(f"  Value counts (INT8 unique): {len(np.unique(data_as_int8))}")

print("\n\n=== Try treating output as INT8 and see if it makes more sense ===")
for si in range(3):
    data_bytes = outputs[si]
    data_as_int8 = np.frombuffer(data_bytes, dtype=np.int8)
    grid = INPUT_SIZE // strides[si]
    total = 85 * grid * grid * 3

    if data_as_int8.size >= total:
        reshaped = data_as_int8.reshape(85, grid, grid, 3)
        print(f"\nScale {si} (as INT8):")
        for ch in range(5):
            vals = reshaped[ch, :, :, :]
            print(f"  ch[{ch}]: min={vals.min()} max={vals.max()} mean={vals.mean():.2f}")

print("\n\n=== Maybe the output isn't [85,g,g,3] but needs different endianness ===")
for si in range(3):
    data_bytes = outputs[si]
    data_as_uint16 = np.frombuffer(data_bytes, dtype=np.uint16)
    print(f"\nScale {si} as uint16: size={data_as_uint16.size}, range=[{data_as_uint16.min()}, {data_as_uint16.max()}]")

print("\n\n=== Final: check if maybe we need to NOT apply sigmoid on class scores ===")
print("=== What if the model already applies sigmoid internally? ===")
all_dets_nosig = []
for si in range(3):
    data = np.frombuffer(outputs[si], dtype=np.float32)
    grid = INPUT_SIZE // strides[si]
    stride = strides[si]
    total = 85 * grid * grid * 3
    if data.size < total: continue
    reshaped = data.reshape(85, grid, grid, 3)
    gx, gy = np.meshgrid(np.arange(grid), np.arange(grid))

    for ai in range(3):
        aw, ah = YOLOV5_ANCHORS[si][ai]
        obj = reshaped[0, :, :, ai]
        tx = reshaped[1, :, :, ai]
        ty = reshaped[2, :, :, ai]
        tw = reshaped[3, :, :, ai]
        th = reshaped[4, :, :, ai]
        cls = reshaped[5:85, :, :, ai].transpose(1,2,0)

        cx = (_sigmoid(tx) * 2 - 0.5 + gx) * stride
        cy = (_sigmoid(ty) * 2 - 0.5 + gy) * stride
        w = (_sigmoid(tw) * 2) ** 2 * aw
        h = (_sigmoid(th) * 2) ** 2 * ah
        max_cls = np.max(cls, axis=-1)
        conf = obj * max_cls
        mask = (conf > 0.2) & (w > 5) & (h > 5)
        indices = np.argwhere(mask)
        for idx in indices:
            yi, xi = int(idx[0]), int(idx[1])
            bw, bh = float(w[yi, xi]), float(h[yi, xi])
            bcx, bcy = float(cx[yi, xi]), float(cy[yi, xi])
            bconf = float(conf[yi, xi])
            cid = int(np.argmax(cls[yi, xi]))
            all_dets_nosig.append(([bcx-bw/2, bcy-bh/2, bcx+bw/2, bcy+bh/2], bconf, cid))

nms_nosig = nms(all_dets_nosig)
print(f"No sigmoid on obj/cls: {len(nms_nosig)} detections")
for d in nms_nosig[:10]:
    box, conf, cid = d
    label = COCO_STANDARD[cid] if cid < len(COCO_STANDARD) else f"c{cid}"
    print(f"  [{cid:2d}] {label}: {conf:.4f} [{box[0]:.0f},{box[1]:.0f},{box[2]:.0f},{box[3]:.0f}]")
