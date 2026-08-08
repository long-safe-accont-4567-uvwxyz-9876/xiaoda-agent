/* bge_npu_runner.c — BGE-small-zh VIP9000 NPU 推理 runner（VIPLite 直调）
 *
 * 加载 NBG 一次，批量推理多条固定 512 输入，输出 CLS 池化 + L2 归一化向量。
 *
 * 用法:
 *   ./bge_npu_runner <nbg> <input.bin> <output.bin> [--batch N] [--seq 512] [--quiet]
 *   ./bge_npu_runner <nbg> --serve [--seq 512] [--quiet]
 *
 * input.bin 布局: N × (3 × seq × 4) 字节，每条为三个 int32[seq] 行优先:
 *   input_ids, attention_mask, token_type_ids （与 gen_bge_calib.py 的 .dat 一致）
 * output.bin 布局: N × 512 × 4 字节 float32（L2 归一化后的 CLS 向量）
 *
 * --serve 常驻流模式: 初始化一次后从 stdin 循环读块（每块 3×seq×4 字节），
 *   每条推理输出 512×float32 向量到 stdout（二进制），EOF 结束；日志走 stderr。
 *
 * 反量化按输出 tensor 的 quant_format/data_format 自动分支
 * （TF_ASYMM: (u8-zp)*scale；DFP: i16/2^pos；NONE: fp32/fp16 直读）。
 */
#define _POSIX_C_SOURCE 200809L
#include <vip_lite.h>
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#define MAX_INPUTS 8
#define MAX_OUTPUTS 8
#define DIM_MAX 6
#define SEQ_DEFAULT 512
#define HID_DEFAULT 512

typedef struct {
    vip_buffer_create_params_t param;
    vip_buffer buf;
    uint32_t elements;
    uint32_t bytes;
} io_slot_t;

static uint64_t now_us(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (uint64_t)tv.tv_sec * 1000000ULL + (uint64_t)tv.tv_usec;
}

static uint32_t elem_count(const vip_buffer_create_params_t *p)
{
    uint32_t c = 1, i;
    for (i = 0; i < p->num_of_dims; i++) c *= p->sizes[i];
    return c;
}

static void print_dims(const vip_buffer_create_params_t *p)
{
    uint32_t i;
    for (i = 0; i < p->num_of_dims; i++)
        printf("%s%u", i ? "x" : "", p->sizes[i]);
}

static void *read_file(const char *path, size_t *out)
{
    FILE *fp = fopen(path, "rb");
    long sz; void *d;
    if (!fp) { fprintf(stderr, "open %s: %s\n", path, strerror(errno)); return NULL; }
    fseek(fp, 0, SEEK_END); sz = ftell(fp);
    if (sz <= 0) { fclose(fp); return NULL; }
    fseek(fp, 0, SEEK_SET);
    d = malloc((size_t)sz);
    if (!d || fread(d, 1, (size_t)sz, fp) != (size_t)sz) { free(d); fclose(fp); return NULL; }
    fclose(fp);
    *out = (size_t)sz;
    return d;
}

static int write_file(const char *path, const void *data, size_t sz)
{
    FILE *fp = fopen(path, "wb");
    if (!fp) { fprintf(stderr, "open %s: %s\n", path, strerror(errno)); return -1; }
    if (fwrite(data, 1, sz, fp) != sz) { fclose(fp); return -1; }
    fclose(fp);
    return 0;
}

static int query_io(vip_network net, int is_in, uint32_t idx, io_slot_t *s)
{
    vip_status_e st;
    memset(&s->param, 0, sizeof(s->param));
    s->param.memory_type = VIP_BUFFER_MEMORY_TYPE_DEFAULT;
    if (is_in) {
        st = vip_query_input(net, idx, VIP_BUFFER_PROP_DATA_FORMAT, &s->param.data_format);
        if (st != VIP_SUCCESS) goto err;
        st = vip_query_input(net, idx, VIP_BUFFER_PROP_NUM_OF_DIMENSION, &s->param.num_of_dims);
        if (st != VIP_SUCCESS) goto err;
        st = vip_query_input(net, idx, VIP_BUFFER_PROP_SIZES_OF_DIMENSION, s->param.sizes);
        if (st != VIP_SUCCESS) goto err;
        st = vip_query_input(net, idx, VIP_BUFFER_PROP_QUANT_FORMAT, &s->param.quant_format);
        if (st != VIP_SUCCESS) goto err;
    } else {
        st = vip_query_output(net, idx, VIP_BUFFER_PROP_DATA_FORMAT, &s->param.data_format);
        if (st != VIP_SUCCESS) goto err;
        st = vip_query_output(net, idx, VIP_BUFFER_PROP_NUM_OF_DIMENSION, &s->param.num_of_dims);
        if (st != VIP_SUCCESS) goto err;
        st = vip_query_output(net, idx, VIP_BUFFER_PROP_SIZES_OF_DIMENSION, s->param.sizes);
        if (st != VIP_SUCCESS) goto err;
        st = vip_query_output(net, idx, VIP_BUFFER_PROP_QUANT_FORMAT, &s->param.quant_format);
        if (st != VIP_SUCCESS) goto err;
    }
    if (s->param.quant_format == VIP_BUFFER_QUANTIZE_TF_ASYMM) {
        if (is_in)
            st = vip_query_input(net, idx, VIP_BUFFER_PROP_TF_SCALE, &s->param.quant_data.affine.scale);
        else
            st = vip_query_output(net, idx, VIP_BUFFER_PROP_TF_SCALE, &s->param.quant_data.affine.scale);
        if (st != VIP_SUCCESS) goto err;
        if (is_in)
            st = vip_query_input(net, idx, VIP_BUFFER_PROP_TF_ZERO_POINT, &s->param.quant_data.affine.zeroPoint);
        else
            st = vip_query_output(net, idx, VIP_BUFFER_PROP_TF_ZERO_POINT, &s->param.quant_data.affine.zeroPoint);
        if (st != VIP_SUCCESS) goto err;
    } else if (s->param.quant_format == VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT) {
        if (is_in)
            st = vip_query_input(net, idx, VIP_BUFFER_PROP_FIXED_POINT_POS, &s->param.quant_data.dfp.fixed_point_pos);
        else
            st = vip_query_output(net, idx, VIP_BUFFER_PROP_FIXED_POINT_POS, &s->param.quant_data.dfp.fixed_point_pos);
        if (st != VIP_SUCCESS) goto err;
    }
    st = vip_create_buffer(&s->param, sizeof(s->param), &s->buf);
    if (st != VIP_SUCCESS) goto err;
    s->elements = elem_count(&s->param);
    s->bytes = vip_get_buffer_size(s->buf);
    return 0;
err:
    fprintf(stderr, "query_io(%s idx=%u) failed: %d\n", is_in ? "in" : "out", idx, st);
    return -1;
}

/* 将输出 tensor 反量化为 float 存到 out（取全部 elements） */
static int dequant_output(const io_slot_t *o, float *out)
{
    vip_flush_buffer(o->buf, VIP_BUFFER_OPER_TYPE_INVALIDATE);
    void *m = vip_map_buffer(o->buf);
    if (!m) { fprintf(stderr, "map output failed\n"); return -1; }
    const vip_buffer_create_params_t *p = &o->param;
    uint32_t n = o->elements;
    if (p->quant_format == VIP_BUFFER_QUANTIZE_TF_ASYMM) {
        const uint8_t *u = (const uint8_t *)m;
        float scale = p->quant_data.affine.scale;
        int zp = p->quant_data.affine.zeroPoint;
        for (uint32_t i = 0; i < n; i++) out[i] = ((float)u[i] - (float)zp) * scale;
    } else if (p->quant_format == VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT) {
        const int16_t *s = (const int16_t *)m;
        float div = (float)(1 << p->quant_data.dfp.fixed_point_pos);
        for (uint32_t i = 0; i < n; i++) out[i] = (float)s[i] / div;
    } else { /* NONE */
        if (p->data_format == VIP_BUFFER_FORMAT_FP32) {
            const float *f = (const float *)m;
            memcpy(out, f, n * sizeof(float));
        } else if (p->data_format == VIP_BUFFER_FORMAT_FP16) {
            const uint16_t *h = (const uint16_t *)m;
            for (uint32_t i = 0; i < n; i++) {
                uint16_t v = h[i];
                uint32_t sign = (uint32_t)(v >> 15) & 1, exp = (uint32_t)(v >> 10) & 0x1f, frac = v & 0x3ff;
                float val;
                if (exp == 0) val = frac ? ldexpf((float)frac, -24) : 0.0f;
                else if (exp == 31) val = frac ? NAN : INFINITY;
                else val = ldexpf(1.0f + (float)frac / 1024.0f, (int)exp - 15);
                out[i] = sign ? -val : val;
            }
        } else if (p->data_format == VIP_BUFFER_FORMAT_INT16) {
            const int16_t *s = (const int16_t *)m;
            for (uint32_t i = 0; i < n; i++) out[i] = (float)s[i];
        } else {
            fprintf(stderr, "unsupported NONE fmt=%d\n", (int)p->data_format);
            vip_unmap_buffer(o->buf);
            return -1;
        }
    }
    vip_unmap_buffer(o->buf);
    return 0;
}

static int set_input_data(const io_slot_t *in, const int32_t *data, uint32_t seq)
{
    void *m = vip_map_buffer(in->buf);
    if (!m) { fprintf(stderr, "map input failed\n"); return -1; }
    memset(m, 0, in->bytes);
    memcpy(m, data, seq * sizeof(int32_t));
    vip_unmap_buffer(in->buf);
    vip_flush_buffer(in->buf, VIP_BUFFER_OPER_TYPE_FLUSH);
    return 0;
}

int main(int argc, char **argv)
{
    const char *nbg_path = NULL, *in_path = NULL, *out_path = NULL;
    uint32_t batch = 1, seq = SEQ_DEFAULT;
    int quiet = 0, dump_full = 0, serve = 0;
    uint32_t n_in = 0, n_out = 0;
    float *full_out = NULL, *vec_out = NULL;
    for (int a = 1; a < argc; a++) {
        if (!strcmp(argv[a], "--batch") && a + 1 < argc) batch = (uint32_t)atoi(argv[++a]);
        else if (!strcmp(argv[a], "--seq") && a + 1 < argc) seq = (uint32_t)atoi(argv[++a]);
        else if (!strcmp(argv[a], "--quiet")) quiet = 1;
        else if (!strcmp(argv[a], "--dump-full")) dump_full = 1;
        else if (!strcmp(argv[a], "--serve")) serve = 1;
        else if (!nbg_path) nbg_path = argv[a];
        else if (!in_path) in_path = argv[a];
        else if (!out_path) out_path = argv[a];
        else { fprintf(stderr, "too many args\n"); return 2; }
    }
    if (!nbg_path || (!serve && (!in_path || !out_path))) {
        fprintf(stderr, "Usage: %s <nbg> <input.bin> <output.bin> [--batch N] [--seq 512] [--quiet]\n", argv[0]);
        fprintf(stderr, "       %s <nbg> --serve [--seq 512] [--quiet]\n", argv[0]);
        return 2;
    }

    size_t in_size = 0;
    int32_t *in_data = NULL;
    uint32_t n = 0;
    if (!serve) {
        in_data = read_file(in_path, &in_size);
        if (!in_data) return 1;
        size_t per = 3u * seq * sizeof(int32_t);
        if (in_size < per * batch) {
            fprintf(stderr, "input size %zu < %zu * %u\n", in_size, per, batch);
            free(in_data);
            return 1;
        }
        n = (uint32_t)(in_size / per);
    }

    uint64_t t0 = now_us();
    if (vip_init() != VIP_SUCCESS) { fprintf(stderr, "vip_init failed\n"); free(in_data); return 1; }
    uint64_t t_init = now_us() - t0;

    vip_network net = NULL;
    t0 = now_us();
    if (vip_create_network((void *)nbg_path, 0, VIP_CREATE_NETWORK_FROM_FILE, &net) != VIP_SUCCESS) {
        fprintf(stderr, "create_network failed\n"); vip_destroy(); free(in_data); return 1;
    }
    uint64_t t_create = now_us() - t0;

    uint32_t device_idx = 0;
    vip_set_network(net, VIP_NETWORK_PROP_SET_DEVICE_INDEX, &device_idx);

    uint32_t ic = 0, oc = 0, cc = 0, mps = 0;
    vip_query_network(net, VIP_NETWORK_PROP_INPUT_COUNT, &ic);
    vip_query_network(net, VIP_NETWORK_PROP_OUTPUT_COUNT, &oc);
    vip_query_network(net, VIP_NETWORK_PROP_CORE_COUNT, &cc);
    vip_query_network(net, VIP_NETWORK_PROP_MEMORY_POOL_SIZE, &mps);
    if (ic > MAX_INPUTS || oc > MAX_OUTPUTS) {
        fprintf(stderr, "too many io %u/%u\n", ic, oc); goto fail;
    }
    n_in = ic; n_out = oc;

    io_slot_t ins[MAX_INPUTS] = {0}, out_slots[MAX_OUTPUTS] = {0};
    for (uint32_t i = 0; i < n_in; i++)
        if (query_io(net, 1, i, &ins[i])) goto fail;
    for (uint32_t i = 0; i < n_out; i++)
        if (query_io(net, 0, i, &out_slots[i])) goto fail;

    t0 = now_us();
    if (vip_prepare_network(net) != VIP_SUCCESS) { fprintf(stderr, "prepare failed\n"); goto fail; }
    uint64_t t_prepare = now_us() - t0;

    for (uint32_t i = 0; i < n_in; i++) vip_set_input(net, i, ins[i].buf);
    for (uint32_t i = 0; i < n_out; i++) vip_set_output(net, i, out_slots[i].buf);

    if (!quiet) {
        printf("=== bge_npu_runner ===\n");
        printf("nbg=%s\n", nbg_path);
        printf("vip_init_us=%" PRIu64 " create_us=%" PRIu64 " prepare_us=%" PRIu64 "\n",
               t_init, t_create, t_prepare);
        printf("io=%u/%u cores=%u mempool=%uB batch=%u seq=%u\n", n_in, n_out, cc, mps, batch, seq);
        for (uint32_t i = 0; i < n_in; i++) {
            printf("in[%u]: ", i); print_dims(&ins[i].param);
            printf(" fmt=%d qfmt=%d elems=%u bytes=%u\n",
                   (int)ins[i].param.data_format, (int)ins[i].param.quant_format,
                   ins[i].elements, ins[i].bytes);
        }
        for (uint32_t i = 0; i < n_out; i++) {
            printf("out[%u]: ", i); print_dims(&out_slots[i].param);
            printf(" fmt=%d qfmt=%d elems=%u bytes=%u", (int)out_slots[i].param.data_format,
                   (int)out_slots[i].param.quant_format, out_slots[i].elements, out_slots[i].bytes);
            if (out_slots[i].param.quant_format == VIP_BUFFER_QUANTIZE_TF_ASYMM)
                printf(" scale=%.6f zp=%d", out_slots[i].param.quant_data.affine.scale,
                       out_slots[i].param.quant_data.affine.zeroPoint);
            if (out_slots[i].param.quant_format == VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT)
                printf(" dfp=%d", out_slots[i].param.quant_data.dfp.fixed_point_pos);
            printf("\n");
        }
    }

    /* 输出缓冲：float 全 tensor + CLS 池化 */
    full_out = malloc((size_t)out_slots[0].elements * sizeof(float));
    vec_out = malloc((serve ? 1u : n) * HID_DEFAULT * sizeof(float));
    if (!full_out || !vec_out) { fprintf(stderr, "malloc fail\n"); goto fail; }

    uint64_t total_npu_us = 0;
    if (serve) {
        /* 常驻流模式：stdin 每块 3×seq×4 字节（三条 int32[seq]），
         * stdout 输出每条 512×float32 的 L2 归一化 CLS 向量（二进制），
         * 所有日志一律走 stderr 以免污染二进制流。 */
        size_t per = 3u * seq * sizeof(int32_t);
        int32_t *blk = malloc(per);
        if (!blk) { fprintf(stderr, "malloc fail\n"); goto fail; }
        setvbuf(stdin, NULL, _IONBF, 0);
        setvbuf(stdout, NULL, _IONBF, 0);
        /* 协议头：库初始化可能在 stdout 打印横幅，Python 端扫描此 magic
         * 丢弃之前内容，其后为纯 512×float32 向量流（每条 2048 字节）。 */
        if (fwrite("BGEVEC01", 1, 8, stdout) != 8) {
            fprintf(stderr, "write magic failed\n"); goto fail;
        }
        fflush(stdout);
        uint32_t idx = 0;
        while (fread(blk, 1, per, stdin) == per) {
            set_input_data(&ins[0], blk + 0 * seq, seq);
            set_input_data(&ins[1], blk + 1 * seq, seq);
            if (n_in > 2) set_input_data(&ins[2], blk + 2 * seq, seq);

            uint64_t tb = now_us();
            if (vip_run_network(net) != VIP_SUCCESS) {
                fprintf(stderr, "run serve idx %u failed\n", idx); goto fail;
            }
            uint64_t wall = now_us() - tb;
            vip_inference_profile_t prof = {0};
            vip_query_network(net, VIP_NETWORK_PROP_PROFILING, &prof);
            total_npu_us += prof.inference_time;

            if (dequant_output(&out_slots[0], full_out)) goto fail;

            float *cls = vec_out;
            memcpy(cls, full_out, HID_DEFAULT * sizeof(float));
            double norm = 0;
            for (int i = 0; i < HID_DEFAULT; i++) norm += (double)cls[i] * cls[i];
            norm = sqrt(norm);
            if (norm > 0) for (int i = 0; i < HID_DEFAULT; i++) cls[i] = (float)((double)cls[i] / norm);

            if (fwrite(cls, sizeof(float), HID_DEFAULT, stdout) != HID_DEFAULT) {
                fprintf(stderr, "write stdout failed\n"); goto fail;
            }
            fflush(stdout);

            if (!quiet) {
                fprintf(stderr, "serve[%u]: npu=%uus wall=%" PRIu64 "us cls[:4]=%.4f %.4f %.4f %.4f norm=%.4f\n",
                        idx, prof.inference_time, wall, cls[0], cls[1], cls[2], cls[3], (float)norm);
            }
            idx++;
        }
        free(blk);
        if (!quiet) fprintf(stderr, "serve done: %u vecs (avg_npu=%.1fus)\n", idx,
                            idx ? (double)total_npu_us / idx : 0.0);
        goto serve_done;
    }

    for (uint32_t b = 0; b < n; b++) {
        const int32_t *base = in_data + (size_t)b * 3 * seq;
        set_input_data(&ins[0], base + 0 * seq, seq);
        set_input_data(&ins[1], base + 1 * seq, seq);
        if (n_in > 2) set_input_data(&ins[2], base + 2 * seq, seq);

        uint64_t tb = now_us();
        if (vip_run_network(net) != VIP_SUCCESS) {
            fprintf(stderr, "run batch %u failed\n", b); goto fail;
        }
        uint64_t wall = now_us() - tb;
        vip_inference_profile_t prof = {0};
        vip_query_network(net, VIP_NETWORK_PROP_PROFILING, &prof);
        total_npu_us += prof.inference_time;

        if (dequant_output(&out_slots[0], full_out)) goto fail;

        /* CLS 池化: tensor[0, :]（行优先第一行 = 前 HID 个值）+ L2 归一化 */
        float *cls = vec_out + (size_t)b * HID_DEFAULT;
        memcpy(cls, full_out, HID_DEFAULT * sizeof(float));
        double norm = 0;
        for (int i = 0; i < HID_DEFAULT; i++) norm += (double)cls[i] * cls[i];
        norm = sqrt(norm);
        if (norm > 0) for (int i = 0; i < HID_DEFAULT; i++) cls[i] = (float)((double)cls[i] / norm);

        if (!quiet) {
            printf("batch[%u]: npu=%uus wall=%" PRIu64 "us", b, prof.inference_time, wall);
            printf(" cls[:4]=%.4f %.4f %.4f %.4f norm=%.4f\n",
                   cls[0], cls[1], cls[2], cls[3], (float)norm);
        }
    }

    if (write_file(out_path, vec_out, n * HID_DEFAULT * sizeof(float))) goto fail;
    if (dump_full) {
        char full_path[1024];
        snprintf(full_path, sizeof(full_path), "%s.full", out_path);
        if (write_file(full_path, full_out, (size_t)out_slots[0].elements * sizeof(float))) goto fail;
        if (!quiet) printf("full dump -> %s (%u floats)\n", full_path, out_slots[0].elements);
    }
    if (!quiet) printf("done: %u vecs -> %s (avg_npu=%.1fus)\n", n, out_path,
                       n ? (double)total_npu_us / n : 0.0);

serve_done:
    free(full_out); free(vec_out);
    for (uint32_t i = 0; i < n_out; i++) vip_destroy_buffer(out_slots[i].buf);
    for (uint32_t i = 0; i < n_in; i++) vip_destroy_buffer(ins[i].buf);
    vip_destroy_network(net);
    vip_destroy();
    free(in_data);
    return 0;

fail:
    free(full_out); free(vec_out);
    for (uint32_t i = 0; i < n_out && out_slots[i].buf; i++) vip_destroy_buffer(out_slots[i].buf);
    for (uint32_t i = 0; i < n_in && ins[i].buf; i++) vip_destroy_buffer(ins[i].buf);
    if (net) vip_destroy_network(net);
    vip_destroy();
    free(in_data);
    return 1;
}
