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

model = NPUModel(MODEL_PATH)
if not model.loaded:
    exit(1)

img = cv2.imread(TEST_IMAGE)
img_r = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE))
img_rgb = cv2.cvtColor(img_r, cv2.COLOR_BGR2RGB)
input_bytes = img_rgb.astype(np.uint8).tobytes()
outputs = model.run(input_bytes)

strides = [8, 16, 32]

print("=== Per-anchor chunk: detailed box analysis ===")
for si in range(3):
    data = np.frombuffer(outputs[si], dtype=np.float32)
    grid = INPUT_SIZE // strides[si]
    stride = strides[si]
    total = 3 * 85 * grid * grid
    if data.size < total:
        continue

    reshaped = data.reshape(3, 85, grid, grid)
    gx, gy = np.meshgrid(np.arange(grid), np.arange(grid))

    print(f"\nScale {si} (stride={stride}, grid={grid}):")
    for ai in range(3):
        aw, ah = YOLOV5_ANCHORS[si][ai]
        chunk = reshaped[ai]

        obj = _sigmoid(chunk[0])
        tx = chunk[1]
        ty = chunk[2]
        tw = chunk[3]
        th = chunk[4]

        cx = (_sigmoid(tx) * 2 - 0.5 + gx) * stride
        cy = (_sigmoid(ty) * 2 - 0.5 + gy) * stride
        w = (_sigmoid(tw) * 2) ** 2 * aw
        h = (_sigmoid(th) * 2) ** 2 * ah

        high_mask = obj > 0.5
        count = np.sum(high_mask)
        if count > 0:
            w_vals = w[high_mask]
            h_vals = h[high_mask]
            print(f"  Anchor {ai} (aw={aw}, ah={ah}): {count} positions with obj>0.5")
            print(f"    w: min={w_vals.min():.4f} max={w_vals.max():.4f} mean={w_vals.mean():.4f}")
            print(f"    h: min={h_vals.min():.4f} max={h_vals.max():.4f} mean={h_vals.mean():.4f}")
            print(f"    tw raw: min={tw[high_mask].min():.4f} max={tw[high_mask].max():.4f}")
            print(f"    th raw: min={th[high_mask].min():.4f} max={th[high_mask].max():.4f}")
            print(f"    tw sig: min={_sigmoid(tw[high_mask]).min():.4f} max={_sigmoid(tw[high_mask]).max():.4f}")
            print(f"    th sig: min={_sigmoid(th[high_mask]).min():.4f} max={_sigmoid(th[high_mask]).max():.4f}")

print("\n\n=== KEY INSIGHT: what if the model output is standard YOLOv5 but")
print("    the data is ALREADY post-processed (no need for sigmoid on coords)? ===")
print("    OR: what if the 85 channels are NOT [obj,tx,ty,tw,th,cls...]")
print("    but [tx,ty,tw,th,obj,cls...] or some other order? ===")

print("\n=== Test: What if we skip sigmoid on tx/ty and only use sigmoid on obj/cls? ===")
for si in range(3):
    data = np.frombuffer(outputs[si], dtype=np.float32)
    grid = INPUT_SIZE // strides[si]
    stride = strides[si]
    total = 3 * 85 * grid * grid
    if data.size < total:
        continue

    reshaped = data.reshape(3, 85, grid, grid)
    gx, gy = np.meshgrid(np.arange(grid), np.arange(grid))

    print(f"\nScale {si} (stride={stride}):")
    for ai in range(3):
        aw, ah = YOLOV5_ANCHORS[si][ai]
        chunk = reshaped[ai]

        for obj_ch in range(5):
            for tx_ch in range(5):
                for ty_ch in range(5):
                    for tw_ch in range(5):
                        for th_ch in range(5):
                            if len({obj_ch, tx_ch, ty_ch, tw_ch, th_ch}) < 5:
                                continue
                            obj = _sigmoid(chunk[obj_ch])
                            tx = chunk[tx_ch]
                            ty = chunk[ty_ch]
                            tw = chunk[tw_ch]
                            th = chunk[th_ch]

                            cx = (_sigmoid(tx) * 2 - 0.5 + gx) * stride
                            cy = (_sigmoid(ty) * 2 - 0.5 + gy) * stride
                            w = (_sigmoid(tw) * 2) ** 2 * aw
                            h = (_sigmoid(th) * 2) ** 2 * ah

                            high_mask = obj > 0.5
                            if not np.any(high_mask):
                                continue
                            w_vals = w[high_mask]
                            h_vals = h[high_mask]
                            if w_vals.max() > 20 and h_vals.max() > 20:
                                print(f"  Anchor {ai}: obj=ch{obj_ch} tx=ch{tx_ch} ty=ch{ty_ch} tw=ch{tw_ch} th=ch{th_ch}")
                                print(f"    w: [{w_vals.min():.1f}, {w_vals.max():.1f}]")
                                print(f"    h: [{h_vals.min():.1f}, {h_vals.max():.1f}]")
