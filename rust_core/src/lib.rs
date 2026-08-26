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
    let mut out: Vec<String> = Vec::new();
    let mut buf: Option<String> = None;
    let mut in_string = false; // 引号外的裸 token / 多余逗号 → 与 json.loads 一致判失败
    let mut escaped = false;
    let mut u16_remaining: u32 = 0; // \uXXXX 剩余待读的 hex 位数
    let mut u16_acc: u32 = 0;
    let mut pending_high_surrogate: Option<u32> = None;
    let mut elements = 0usize; // 已闭合的字符串元素数
    let mut commas = 0usize;   // 元素间逗号数（合法：elements>0 时 commas==elements-1）
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
                // 非法 hex：Python json.loads 同样拒绝
                return None;
            }
            continue;
        }
        if escaped {
            // 上一个字符是反斜杠：转义序列（与 Python json 合法集合一致：
            // " \ / b f n r t u；其余为非法转义，json.loads 拒绝，此处同样拒绝）
            match ch {
                'n' => push_char(&mut buf, '\n'),
                't' => push_char(&mut buf, '\t'),
                'r' => push_char(&mut buf, '\r'),
                'b' => push_char(&mut buf, '\u{8}'),
                'f' => push_char(&mut buf, '\u{c}'),
                'u' => u16_remaining = 4,
                '"' => push_char(&mut buf, '"'),
                '\\' => push_char(&mut buf, '\\'),
                '/' => push_char(&mut buf, '/'),
                _ => return None,
            }
            escaped = false;
        } else if in_string {
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
                    in_string = false;
                    elements += 1;
                }
                other => push_char(&mut buf, other),
            }
        } else {
            match ch {
                '"' => {
                    in_string = true;
                    buf = Some(String::new());
                }
                c if c.is_whitespace() => {}
                ',' => commas += 1,
                _ => return None, // 引号外裸 token：json.loads 拒绝，此处同样拒绝
            }
        }
    }
    if in_string {
        return None; // 未闭合引号
    }
    // 尾逗号/连续逗号：commas 必须恰好是 elements-1（空数组两者皆 0）
    if commas != elements.saturating_sub(1) {
        return None;
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
    /// 全量 key 文档频率（new() 一次预计算）：key -> 含该 key 的节点数。
    /// keys 加载后恒不变，df 是静态统计--每查询重算等于把 O(预计算) 干成 O(每查询)
    key_df: HashMap<Box<str>, usize>,
    /// 驻留边快照（load_edges 后可用）：邻接表按节点下标组织
    /// adj[i] = Vec<(neighbor_idx, weight)>，图游走零字符串哈希
    adj: Vec<Vec<(usize, f64)>>,
    /// id -> 下标（new() 一次性构建；ids 构造后恒不变，
    /// 边快照刷新无需重建，省去每次 load_edges 的全量 String 克隆）
    id_index: HashMap<String, usize>,
    /// 边快照是否已加载
    has_graph: bool,
}

impl NodeIndex {
    /// direct_channel 的纯计算段（不接触任何 Python 对象，可在无 GIL 下执行）。
    fn direct_scores<'a>(&'a self, query: &str, query_keys: &[String])
        -> HashMap<&'a str, f64> {
        let n = self.ids.len() as f64;
        let qset: HashSet<&str> = query_keys.iter().map(|s| s.as_str()).collect();
        // IDF：df 已在 new() 全量预计算，此处仅 |query_keys| 次查表
        let idf: HashMap<&str, f64> = query_keys
            .iter()
            .map(|k| {
                let d = self.key_df.get(k.as_str()).copied().unwrap_or(0) as f64;
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

            // 与 Python 一致：交集非空即写入条目（idf 可能为负，负值也要保留）
            let has_shared = nk.iter().any(|k| qset.contains(k.as_str()));
            if has_shared {
                let shared_score: f64 = nk
                    .iter()
                    .filter(|k| qset.contains(k.as_str()))
                    .map(|k| idf.get(k.as_str()).copied().unwrap_or(0.0))
                    .sum();
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
        direct
    }

    /// spreading_channel 的纯计算段（不接触任何 Python 对象，可在无 GIL 下执行）。
    /// 返回 (被写入过的下标及其激活值, 未识别种子的合并累积)。
    fn spreading_scores<'a>(&'a self, seeds: &'a [(String, f64)], radius: usize,
                            decay: f64, threshold: f64)
        -> (Vec<(&'a str, f64)>, HashMap<&'a str, f64>) {
        // 种子入队；不在节点集的种子按 Python 语义仅累积自身激活、不传播
        // （生产路径种子恒来自 direct_channel ⊆ 节点集，此分支纯防御）
        let mut spread = vec![0.0f64; self.ids.len()];
        // 位图记录被 defaultdict 写入过的下标（含 0 值）--HashSet per-op 哈希开销更高
        let mut touched = vec![false; self.ids.len()];
        let mut wave: HashMap<usize, f64> = HashMap::new();
        let mut unknown: HashMap<&str, f64> = HashMap::new();
        for (nid, act) in seeds {
            if let Some(&idx) = self.id_index.get(nid.as_str()) {
                wave.insert(idx, *act);
            } else {
                // 同 id 多次出现时合并累积（保持与 dict 累加一致）
                *unknown.entry(nid.as_str()).or_insert(0.0) += *act;
            }
        }

        let mut current = wave;
        for hop in 0..=radius {
            let mut next: HashMap<usize, f64> = HashMap::new();
            for (&idx, &act) in current.iter() {
                // spread[idx] 初始化即为 0.0：写入即保留（含 0 值种子条目）
                touched[idx] = true;
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

        let pairs: Vec<(&str, f64)> = touched
            .iter()
            .enumerate()
            .filter(|(_, &t)| t)
            .map(|(i, _)| (self.ids[i].as_str(), spread[i]))
            .collect();
        (pairs, unknown)
    }
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
        // 全量 df 预计算：静态数据一次统计，direct_channel 每查询仅查表
        let mut key_df: HashMap<Box<str>, usize> = HashMap::new();
        for nk in &keys {
            for k in nk {
                *key_df.entry(k.as_str().into()).or_insert(0) += 1;
            }
        }
        // id -> 下标一次性构建：ids 构造后恒不变，load_edges 不再重建
        let mut id_index: HashMap<String, usize> = HashMap::with_capacity(n);
        for (i, id) in node_ids.iter().enumerate() {
            id_index.insert(id.clone(), i);
        }
        Ok(NodeIndex {
            ids: node_ids,
            keys,
            texts_lower,
            weights: node_weights,
            key_df,
            adj: vec![Vec::new(); n],
            id_index,
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
    ///
    /// 纯计算段在 py.allow_threads 内无 GIL 执行：7ms 的 IDF+打分不再冻结
    /// 同进程所有 Python 线程（QQ/Web 双通道共享进程的尾延迟改善），
    /// 结束后回 GIL 组装 PyDict。
    fn direct_channel(&self, py: Python<'_>, query: &str, query_keys: Vec<String>) -> Py<PyDict> {
        // 关键：计算只借用 &self（NodeIndex 字段全是 Send 数据，跨线程安全），
        // 零 Python 对象接触；q_lower/to_lowercase 也在闭包内完成
        let direct: Vec<(&str, f64)> = py
            .allow_threads(|| self.direct_scores(query, &query_keys))
            .into_iter()
            .collect();

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
        // id_index 已在 new() 构建且 ids 恒不变，此处只重置邻接表
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

    /// 扩散激活通道：从种子沿边传播激活值。
    ///
    /// 语义与 spreading_activation._spreading_channel 等价（容差 1e-9 相对
    /// 误差——两侧浮点求和顺序不同，不满足结合律，不可能逐位一致；
    /// tests/test_rust_hybrid_poc.py 以相对误差断言）：
    /// - spread[nid] += act（含种子首跳）；传播条件 hop < radius 且 act > threshold
    /// - propagated = act * decay * edge_weight / (hop + 1)；邻居必须已驻留
    /// - seeds: [(node_id, activation)]；返回 {node_id: accumulated}
    ///
    /// 实测（2417 节点 / 100 万边）：游走 26.4x 于 Python 版（842.7→31.9ms），
    /// 最大相对误差 3.09e-15。
    ///
    /// 图游走在 py.allow_threads 内无 GIL 执行：~32ms 的波前游走不再冻结
    /// 同进程所有 Python 线程，结束后回 GIL 组装 PyDict。
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
        // 计算只借用 &self 与 &seeds（字段全为 Send 数据），零 Python 对象接触
        let (known, unknown): (Vec<(&str, f64)>, HashMap<&str, f64>) = py
            .allow_threads(|| self.spreading_scores(&seeds, radius, decay, threshold));

        let out = PyDict::new(py);
        for (nid, v) in known {
            let _ = out.set_item(nid, v);
        }
        for (nid, v) in unknown {
            let _ = out.set_item(nid, v);
        }
        Ok(out.into())
    }
}

#[pymodule]
fn rust_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // 二进制契约版本：与 memory/rust_hybrid.py 的 RUST_CORE_CONTRACT_VERSION
    // 必须相等，任何 pyclass/pymethod 增删改（含语义变化）双侧同步 bump。
    // Python 侧 _try_import 校验此值+符号表，不符视同扩展不可用（回退纯 Python），
    // 防止陈旧 .so 在使用点爆 AttributeError。
    m.add("CONTRACT_VERSION", 3)?;
    m.add_class::<NodeIndex>()?;
    Ok(())
}
