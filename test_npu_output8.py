#!/usr/bin/env python3
import numpy as np
import cv2
import sys
import os
import struct

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
img = cv2.imread(TEST_IMAGE)
img_r = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE))
img_rgb = cv2.cvtColor(img_r, cv2.COLOR_BGR2RGB)
input_bytes = img_rgb.astype(np.uint8).tobytes()
outputs = model.run(input_bytes)

print("=== Check byte-level patterns in output 0 ===")
data_bytes = outputs[0]
print(f"Total bytes: {len(data_bytes)}")

first_100_floats = np.frombuffer(data_bytes[:400], dtype=np.float32)
print(f"\nFirst 20 floats (hex + value):")
for i in range(20):
    b = data_bytes[i*4:(i+1)*4]
    f = struct.unpack('f', b)[0]
    hex_str = b.hex()
    print(f"  [{i:3d}] 0x{hex_str} = {f:.6f}")

print("\n=== Check if data might be BFloat16 (2 bytes per value) ===")
data_bf16 = np.frombuffer(data_bytes, dtype=np.uint16)
print(f"Total uint16 values: {data_bf16.size}")
# BFloat16: upper 16 bits of float32 = FP32 with lower 16 bits truncated
# If data is BF16, each pair of bytes should look like the upper half of a float32
# Let's check the first few values
print("\nFirst 20 uint16 values:")
for i in range(20):
    b = data_bytes[i*2:(i+1)*2]
    u16 = struct.unpack('H', b)[0]
    print(f"  [{i}] 0x{u16:04x}")

# Compare: first 20 FP32 values - check upper 16 bits of each
print("\nCompare FP32 upper 16 bits with uint16 interpretation:")
for i in range(10):
    b4 = data_bytes[i*4:(i+1)*4]
    f32 = struct.unpack('f', b4)[0]
    upper_16 = struct.unpack('H', b4[2:4])[0]
    u16_direct = struct.unpack('H', data_bytes[i*2:(i+1)*2])[0]
    print(f"  [{i}] FP32={f32:.6f} upper16=0x{upper_16:04x} direct_u16=0x{u16_direct:04x}")

print("\n=== Check if there's BFloat16 packed as uint16 ===")
# Try interpreting pairs of uint16 as BF16 -> FP32
bf16_as_fp32 = np.zeros(len(data_bf16) // 2, dtype=np.float32)
for i in range(min(20, len(data_bf16) // 2)):
    u16 = data_bf16[i]
    # BF16 -> FP32: shift left 16 bits
    fp32_bits = (u16.astype(np.uint32)) << 16
    f32 = np.frombuffer(fp32_bits.tobytes(), dtype=np.float32)[0]
    bf16_as_fp32[i] = f32

print(f"BF16 -> FP32 first 20 values: {bf16_as_fp32[:20]}")
print(f"BF16 -> FP32 range: [{bf16_as_fp32.min():.4f}, {bf16_as_fp32.max():.4f}]")

print("\n=== Check: maybe the output is already INT8 and needs affine dequantization ===")
print("Input scale=1/255, zero_point=0 -> standard [0,1] normalization")
print("Output scale=1.0, zero_point=0 -> raw FP32")

print("\n=== The real question: what is the actual layout of the 85 values per grid cell? ===")
print("Let's print the raw values for a high-objectness cell in all possible interpretations")
data = np.frombuffer(outputs[0], dtype=np.float32)

# Cell with highest objectness: grid[53,39] in anchor 0 of (85,80,80,3) layout
# idx = 53*80 + 39 = 4279 in the grid
# For anchor 0: offset = 0
# Each (cell, channel) pair: value at position channel * 80 * 80 * 3 + (53*80+39)*3 + 0

print("\nGrid cell [53,39] anchor 0, all 85 channels (as 85,g,g,3 reshape):")
reshaped = data.reshape(85, 80, 80, 3)
for ch in range(85):
    val = reshaped[ch, 53, 39, 0]
    print(f"  ch[{ch:2d}] = {val:8.4f}  sig={_sigmoid(val):.4f}")

print("\n=== What if the model uses a DIFFERENT anchor set? ===")
print("Let's try with no anchors (direct box prediction) and see if values make sense as direct predictions")
reshaped85 = data.reshape(85, 80, 80, 3)
for ai in range(3):
    print(f"\nAnchor {ai}:")
    obj = _sigmoid(reshaped85[0, :, :, ai])
    high_mask = obj > 0.5
    if not np.any(high_mask):
        continue
    idx = np.argwhere(high_mask)[0]
    yi, xi = int(idx[0]), int(idx[1])
    print(f"  Grid[{yi},{xi}]:")
    for ch in range(5):
        raw = reshaped85[ch, yi, xi, ai]
        sig = _sigmoid(raw)
        print(f"    ch[{ch}] raw={raw:.4f} sigmoid={sig:.4f}")
    cls_raw = reshaped85[5:85, yi, xi, ai]
    cls_sig = _sigmoid(cls_raw)
    top3 = np.argsort(cls_sig)[::-1][:3]
    for t in top3:
        print(f"    cls[{t:2d}]={COCO_STANDARD[t]:>16s} raw={cls_raw[t]:.4f} sig={cls_sig[t]:.4f}")
