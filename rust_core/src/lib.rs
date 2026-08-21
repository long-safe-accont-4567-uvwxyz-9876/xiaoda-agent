//! rust_core — Xiaoda Agent CPU 热点下沉 PoC（perf/rust-hybrid-poc）
//!
//! 首个下沉目标：扩散激活通道的 `_compute_idf` + `_direct_channel`。
//! 选型依据（本机 aarch64 实测，2026-08-21）：
//!   - _direct_channel 37.5ms/查询 + _compute_idf 16.7ms/查询（2417 节点真实规模），
//!     纯解释器循环（json.loads + set 运算 + 子串扫描），无外部 IO；
//!   - jieba 分词 0.09ms、numpy 余弦批量 4.9ms——非瓶颈，不下沉；
//!   - embed(3.5s)/KG(3s) 是 NPU 进程排队与 LLM 网络调用——Rust 无收益。
//!
//! 语义契约：与 memory/spreading_activation.py 的 Python 实现逐字段对齐
//! （weight_bias floor 0.35、子串 len>=4 双向计分 0.6 系数、IDF 公式
//! ln(N/(1+df))），由 tests/test_rust_hybrid_poc.py 做等价性验证。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::{HashMap, HashSet};

/// 解析节点 keys 字段（JSON 数组字符串），失败返回 None——与 Python
/// `except (JSONDecodeError, TypeError): node_keys = set()` 行为一致。
/// 手写轻量解析：keys 字段恒为 JSON 字符串数组（Python 端 json.dumps(list[str])），
/// 零第三方 crate 依赖，编译时间 <30s。
fn parse_json_keys(s: &str) -> Option<Vec<String>> {
    let s = s.trim();
    if !s.starts_with('[') || !s.ends_with(']') {
        return None;
    }
    let inner = &s[1..s.len() - 1];
    let mut out = Vec::new();
    let mut buf: Option<String> = None;
    let mut escaped = false;
    let mut u16_remaining: u32 = 0; // \uXXXX 剩余待读的 hex 位数
    let mut u16_acc: u32 = 0;
    let mut pending_high_surrogate: Option<u32> = None;
    for ch in inner.chars() {
        if u16_remaining > 0 {
            // 收集 \uXXXX 的 4 位 hex
            if let Some(d) = ch.to_digit(16) {
                u16_acc = (u16_acc << 4) | d;
                u16_remaining -= 1;
                if u16_remaining == 0 {
                    let cp = u16_acc;
                    u16_acc = 0;
                    if (0xD800..0xDC00).contains(&cp) {
                        // 高代理：暂存，期待紧跟低代理 \uDC00-\uDFFF
                        pending_high_surrogate = Some(cp);
                    } else if (0xDC00..0xE000).contains(&cp) {
                        // 低代理：与暂存的高代理合成码点
                        let combined = pending_high_surrogate
                            .take()
                            .map(|hi| 0x10000 + ((hi - 0xD800) << 10) + (cp - 0xDC00))
                            .unwrap_or(0xFFFD);
                        push_codepoint(&mut buf, combined);
                    } else {
                        // 若之前有孤立高代理，先补替换符
                        if pending_high_surrogate.take().is_some() {
                            push_codepoint(&mut buf, 0xFFFD);
                        }
                        push_codepoint(&mut buf, cp);
                    }
                }
            } else {
                // 非法 hex：按 Python json 容错性直接整体判失败更安全
                return None;
            }
            continue;
        }
        if escaped {
            // 上一个字符是反斜杠：转义序列
            match ch {
                'n' => push_char(&mut buf, '\n'),
                't' => push_char(&mut buf, '\t'),
                'r' => push_char(&mut buf, '\r'),
                'u' => u16_remaining = 4,
                other => push_char(&mut buf, other),
            }
            escaped = false;
        } else {
            match ch {
                '\\' => escaped = true,
                '"' => {
                    // 字符串边界：收尾一个完整 token；孤立高代理补替换符
                    if pending_high_surrogate.take().is_some() {
                        push_codepoint(&mut buf, 0xFFFD);
                    }
                    if let Some(b) = buf.take() {
                        out.push(b);
                    }
                }
                other => {
                    if let Some(b) = buf.as_mut() {
                        b.push(other);
                    } else {
                        buf = Some(String::from(other));
                    }
                }
            }
        }
    }
    Some(out)
}

fn push_char(buf: &mut Option<String>, c: char) {
    if let Some(b) = buf.as_mut() {
        b.push(c);
    } else {
        *buf = Some(String::from(c));
    }
}

fn push_codepoint(buf: &mut Option<String>, cp: u32) {
    let c = char::from_u32(cp).unwrap_or('\u{FFFD}');
    push_char(buf, c);
}

/// 常驻节点索引：数据一次加载驻留 Rust 侧，每查询零数据拷贝。
///
/// 实测依据（2026-08-21 aarch64）：每次调用跨 FFI 拷贝 2400×(id+keys_json+text+weight)
/// 开销 ~83ms，超过 Python 纯计算 53ms——无状态调用模式净亏 0.6x。
/// 常驻模式下 Python→Rust 每查询只传 query+keys（微秒级），且 json.loads×2400(31ms)
/// 与 lower()×2400(12ms) 的重复预处理在加载时一次性完成。
#[pyclass]
struct NodeIndex {
    ids: Vec<String>,
    /// 预解析 keys（加载时一次 JSON 解析）
    keys: Vec<HashSet<String>>,
    /// 预 lowercase 文本（加载时一次）
    texts_lower: Vec<String>,
    weights: Vec<f64>,
    /// 驻留边快照（load_edges 后可用）：邻接表按节点下标组织
    /// adj[i] = Vec<(neighbor_idx, weight)>，图游走零字符串哈希
    adj: Vec<Vec<(usize, f64)>>,
    /// id → 下标（load_edges 时构建）
    id_index: HashMap<String, usize>,
    /// 边快照是否已加载
    has_graph: bool,
}

#[pymethods]
impl NodeIndex {
    #[new]
    #[pyo3(signature = (node_ids, node_keys_json, node_texts, node_weights))]
    fn new(
        node_ids: Vec<String>,
        node_keys_json: Vec<String>,
        node_texts: Vec<String>,
        node_weights: Vec<f64>,
    ) -> PyResult<Self> {
        if node_ids.len() != node_keys_json.len()
            || node_ids.len() != node_texts.len()
            || node_ids.len() != node_weights.len()
        {
            return Err(PyValueError::new_err(
                "node_ids/node_keys_json/node_texts/node_weights length mismatch",
            ));
        }
        let keys: Vec<HashSet<String>> = node_keys_json
            .iter()
            .map(|s| parse_json_keys(s).unwrap_or_default().into_iter().collect())
            .collect();
        let texts_lower: Vec<String> = node_texts.iter().map(|t| t.to_lowercase()).collect();
        let n = node_ids.len();
        Ok(NodeIndex {
            ids: node_ids,
            keys,
            texts_lower,
            weights: node_weights,
            adj: vec![Vec::new(); n],
            id_index: HashMap::with_capacity(n),
            has_graph: false,
        })
    }

    /// 节点数（健康检查用）
    #[getter]
    fn size(&self) -> usize {
        self.ids.len()
    }

    /// 直接命中通道：IDF 加权 key 重叠 + 双向子串包含（语义与无状态版逐位一致）。
    /// 返回 {node_id: score}，仅含得分 > 0 的节点。
    fn direct_channel(&self, py: Python<'_>, query: &str, query_keys: Vec<String>) -> Py<PyDict> {
        // IDF（节点数据已驻留，无需重复解析）
        let n = self.ids.len() as f64;
        let qset: HashSet<&str> = query_keys.iter().map(|s| s.as_str()).collect();
        let mut df: HashMap<&str, usize> = HashMap::new();
        for nk in &self.keys {
            for k in nk {
                if qset.contains(k.as_str()) {
                    *df.entry(Box::leak(k.clone().into_boxed_str())).or_insert(0) += 1;
                }
            }
        }
        let idf: HashMap<&str, f64> = query_keys
            .iter()
            .map(|k| {
                let d = *df.get(k.as_str()).unwrap_or(&0) as f64;
                (k.as_str(), (n / (1.0 + d)).ln())
            })
            .collect();

        let long_keys: Vec<&str> = query_keys
            .iter()
            .map(|s| s.as_str())
            .filter(|k| k.chars().count() >= 4)
            .collect();
        let q_lower = query.to_lowercase();

        let mut direct: HashMap<&str, f64> = HashMap::new();
        for (i, nid) in self.ids.iter().enumerate() {
            let nk = &self.keys[i];
            let w_bias = 0.35 + 0.65 * self.weights[i];

            let shared_score: f64 = nk
                .iter()
                .filter(|k| qset.contains(k.as_str()))
                .map(|k| idf.get(k.as_str()).copied().unwrap_or(0.0))
                .sum();
            if shared_score > 0.0 {
                *direct.entry(nid.as_str()).or_insert(0.0) += shared_score * w_bias;
            }

            let n_text = &self.texts_lower[i];
            let substr = long_keys.iter().filter(|w| n_text.contains(**w)).count() as f64;
            let reverse = nk
                .iter()
                .filter(|k| k.chars().count() >= 4 && q_lower.contains(k.as_str()))
                .count() as f64;
            if substr + reverse > 0.0 {
                *direct.entry(nid.as_str()).or_insert(0.0) +=
                    (substr + reverse) * 0.6 * w_bias;
            }
        }

        let out = PyDict::new(py);
        for (nid, score) in direct {
            let _ = out.set_item(nid, score);
        }
        out.into()
    }

    /// 驻留边快照：rows = [(source_id, target_id, weight), ...]。
    /// 一次性把 Python 行列表转成按下标组织的邻接表，之后图游走
    /// 全程 usize 下标 + f64 运算，零字符串哈希、零跨语言拷贝。
    /// 两端 id 均不在节点集的边跳过（与 Python 版 alive 过滤等价——
    /// 游走时邻居必须 alive 才入队，加载期预过滤结果一致）。
    #[pyo3(signature = (rows,))]
    fn load_edges(&mut self, rows: Vec<(String, String, f64)>) -> PyResult<usize> {
        self.id_index.clear();
        for (i, id) in self.ids.iter().enumerate() {
            self.id_index.insert(id.clone(), i);
        }
        for v in self.adj.iter_mut() {
            v.clear();
        }
        let mut kept = 0usize;
        for (src, dst, w) in rows {
            let (si, di) = match (self.id_index.get(&src), self.id_index.get(&dst)) {
                (Some(&s), Some(&d)) => (s, d),
                _ => continue,
            };
            self.adj[si].push((di, w));
            kept += 1;
        }
        self.has_graph = true;
        Ok(kept)
    }

    /// 扩散激活通道：从种子沿边传播激活值（语义与
    /// spreading_activation._spreading_channel 逐位一致）：
    /// spread[nid] += act（含种子）；传播条件 hop < radius 且 act > threshold；
    /// propagated = act * decay * edge_weight / (hop + 1)；邻居必须已驻留。
    /// seeds: [(node_id, activation)]；返回 {node_id: accumulated}。
    #[pyo3(signature = (seeds, radius=3, decay=0.5, threshold=0.05))]
    fn spreading_channel(
        &self,
        py: Python<'_>,
        seeds: Vec<(String, f64)>,
        radius: usize,
        decay: f64,
        threshold: f64,
    ) -> PyResult<Py<PyDict>> {
        if !self.has_graph {
            return Err(PyValueError::new_err("edges not loaded; call load_edges first"));
        }
        // 种子映射为下标激活（不在节点集的种子忽略——Python 版 graph.get(nid,{})
        // 对未知 nid 无边可走，仅 spread[nid]+=act；此处保持一致：先记录再判断）
        let mut spread = vec![0.0f64; self.ids.len()];
        let mut wave: HashMap<usize, f64> = HashMap::new();
        for (nid, act) in &seeds {
            if let Some(&i) = self.id_index.get(nid) {
                // 注意：种子激活只在 hop0 循环里 spread[idx] += act 累积一次
                //（与 Python 版一致），此处仅入队，不预累积
                wave.insert(i, *act);
            } else {
                // 与 Python 一致：未知种子也累积进 spread 输出
                // （graph.get(nid, {}) 为空 → 不传播）
                let _ = nid; // 输出阶段统一处理
            }
        }
        // 未知种子的累积：单独记录（数量极少，通常 0）
        let mut unknown_spread: HashMap<&str, f64> = HashMap::new();
        for (nid, act) in &seeds {
            if !self.id_index.contains_key(nid) {
                *unknown_spread.entry(nid.as_str()).or_insert(0.0) += act;
            }
        }

        let mut current = wave;
        for hop in 0..=radius {
            let mut next: HashMap<usize, f64> = HashMap::new();
            for (&idx, &act) in current.iter() {
                spread[idx] += act;
                if hop < radius && act > threshold {
                    // adj 与 ids 同长，idx 恒在界内；用安全索引避免 unsafe
                    if let Some(edges) = self.adj.get(idx) {
                        for &(nb, ew) in edges {
                            let propagated = act * decay * ew / ((hop + 1) as f64);
                            *next.entry(nb).or_insert(0.0) += propagated;
                        }
                    }
                }
            }
            current = next;
            if current.is_empty() {
                break;
            }
        }

        let out = PyDict::new(py);
        for (i, &v) in spread.iter().enumerate() {
            if v != 0.0 {
                let _ = out.set_item(self.ids[i].as_str(), v);
            }
        }
        for (nid, v) in unknown_spread {
            let _ = out.set_item(nid, v);
        }
        Ok(out.into())
    }
}

#[pymodule]
fn rust_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NodeIndex>()?;
    Ok(())
}
