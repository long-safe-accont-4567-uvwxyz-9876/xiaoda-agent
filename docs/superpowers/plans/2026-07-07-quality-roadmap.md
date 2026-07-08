# Quality Roadmap 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依次完成5项质量提升：收窄qq_bot_adapter宽异常、收窄core/db/dispatcher宽异常、添加trace ID、固化测试依赖、添加K8s配置

**Architecture:** 按优先级从高到低逐项实施，每项独立可测试可提交。异常收窄遵循"读上下文→确定具体异常→替换→验证"流程。trace ID通过中间件注入ContextVar贯穿日志和指标。

**Tech Stack:** Python 3.11+, asyncio, loguru, FastAPI, contextvars, pytest, Kubernetes (YAML)

## Global Constraints

- 所有 `except Exception` 必须收窄为具体异常类型（如 `OSError`, `RuntimeError`, `KeyError`, `ValueError`, `asyncio.CancelledError` 等）
- 收窄后必须保留原有日志记录，不得静默吞异常
- 不改变函数签名和外部行为
- 每个文件修改后必须通过 `python -m py_compile` 验证
- 不引入新依赖（trace ID 用标准库 `contextvars`）
- Kubernetes 配置使用标准资源：Deployment + Service + ConfigMap + Secret

---

### Task 1: 收窄 qq_bot_adapter.py 的 17 处 except Exception

**Files:**
- Modify: `qq_bot_adapter.py` (17处 except Exception)

**Interfaces:**
- Consumes: 无外部依赖
- Produces: 更健壮的异常处理，不影响调用方

- [ ] **Step 1: 收窄 botpy 会话重连相关 (L151, L156, L173)**

L151: `except Exception as login_err:` → `except (OSError, RuntimeError, ConnectionError) as login_err:`
L156: `except Exception as e:` (外层会话循环) → `except (OSError, RuntimeError, ConnectionError, asyncio.TimeoutError) as e:`
L173: `except Exception as login_err:` → `except (OSError, RuntimeError, ConnectionError) as login_err:`

- [ ] **Step 2: 收窄 run_qq_bot 重连循环 (L277, L280)**

L277: `except Exception as e:` (close on cancel) → `except (OSError, RuntimeError) as e:`
L280: `except Exception as e:` (crashed retrying) → `except (OSError, RuntimeError, ConnectionError, asyncio.TimeoutError) as e:`

- [ ] **Step 3: 收窄 _get_config_service (L327)**

L327: `except Exception:` → `except (ImportError, AttributeError):`

- [ ] **Step 4: 收窄 on_ready nudge 初始化 (L360)**

L360: `except Exception as e:` → `except (ImportError, AttributeError, OSError, RuntimeError) as e:`

- [ ] **Step 5: 收窄 _send_approval_message (L384)**

L384: `except Exception as e:` → `except (OSError, RuntimeError, ConnectionError) as e:`

- [ ] **Step 6: 收窄 _process_message_attachments 图片编码 (L462)**

L462: `except Exception as e:` → `except (OSError, ValueError, RuntimeError) as e:`

- [ ] **Step 7: 收窄 _get_or_create_c2c_session (L548)**

L548: `except Exception as e:` → `except (KeyError, OSError, RuntimeError) as e:`

- [ ] **Step 8: 收窄 _process_c2c_reply 超时回退 (L593, L595, L599)**

L593: `except Exception as _e:` (timeout reply) → `except (OSError, RuntimeError, ConnectionError) as _e:`
L595: `except Exception as e:` (c2c error) → `except (RuntimeError, OSError, asyncio.TimeoutError, ValueError) as e:`
L599: `except Exception as e:` (fallback reply) → `except (OSError, RuntimeError, ConnectionError) as e:`

- [ ] **Step 9: 收窄 on_group_at_message_create ACK/超时/回退 (L654, L674, L676, L680)**

L654: `except Exception as e:` (ack send) → `except (OSError, RuntimeError, ConnectionError) as e:`
L674: `except Exception as _e:` (group timeout reply) → `except (OSError, RuntimeError, ConnectionError) as _e:`
L676: `except Exception as e:` (group error) → `except (RuntimeError, OSError, asyncio.TimeoutError, ValueError) as e:`
L680: `except Exception as e2:` (group fallback) → `except (OSError, RuntimeError, ConnectionError) as e2:`

- [ ] **Step 10: 收窄 _send_reply_with_media (L721, L730, L735)**

L721: `except Exception as e:` (group media passive limited) → `except (OSError, RuntimeError, ConnectionError) as e:`
L730: `except Exception as e:` (media send failed) → `except (OSError, RuntimeError, ConnectionError, ValueError) as e:`
L735: `except Exception as _e:` (fallback reply) → `except (OSError, RuntimeError, ConnectionError) as _e:`

- [ ] **Step 11: 收窄 _upload_c2c_base64 重试 (L773, L785)**

L773: `except Exception as e:` (upload retry) → `except (OSError, RuntimeError, ConnectionError, TimeoutError) as e:`
L785: `except Exception as e:` (temp cleanup) → `except OSError as e:`

- [ ] **Step 12: 收窄 _upload_group_base64 重试 (同结构两处)**

同样结构的 group 上传重试和临时文件清理，收窄为同上具体异常。

- [ ] **Step 13: 编译验证**

Run: `python -m py_compile qq_bot_adapter.py`
Expected: 无错误输出

- [ ] **Step 14: 提交**

```bash
git add qq_bot_adapter.py
git commit -m "fix: 收窄qq_bot_adapter.py 17处except Exception为具体异常类型"
```

---

### Task 2: 收窄 core/background_tasks.py 的 17 处 except Exception

**Files:**
- Modify: `core/background_tasks.py` (17处 except Exception)

**Interfaces:**
- Consumes: 无外部依赖
- Produces: 更健壮的后台任务异常处理

- [ ] **Step 1: 收窄 _run_persistence_tasks (L132, L140, L147, L166)**

L132: `except Exception as e:` (conversation_log) → `except (OSError, ValueError, RuntimeError) as e:`
L140: `except Exception as e:` (session_update) → `except (KeyError, OSError, RuntimeError) as e:`
L147: `except Exception as e:` (batch_commit) → `except (OSError, RuntimeError) as e:`
L166: `except Exception as e:` (memory_encode) → `except (OSError, ValueError, RuntimeError) as e:`

- [ ] **Step 2: 收窄 _run_scheduled_tasks (L216, L223, L232, L242, L249)**

L216: `except Exception as e:` (dream_archive) → `except (ImportError, OSError, RuntimeError) as e:`
L223: `except Exception as e:` (warm_embedding_cache) → `except (ImportError, OSError, RuntimeError) as e:`
L232: `except Exception as e:` (memory_distill) → `except (ImportError, OSError, RuntimeError) as e:`
L242: `except Exception as e:` (learning_promote) → `except (ImportError, OSError, RuntimeError) as e:`
L249: `except Exception as e:` (mail_token_refresh) → `except (ImportError, OSError, RuntimeError) as e:`

- [ ] **Step 3: 收窄 _auto_archive_sessions (L257)**

L257: `except Exception as e:` → `except (OSError, RuntimeError) as e:`

- [ ] **Step 4: 收窄 _portrait_cold_start (L267)**

L267: `except Exception as e:` → `except (OSError, ValueError, RuntimeError) as e:`

- [ ] **Step 5: 收窄 _should_run (L277)**

L277: `except Exception:` → `except (OSError, RuntimeError):`

- [ ] **Step 6: 收窄 _dream_archive_task (L297)**

L297: `except Exception as e:` → `except (ImportError, OSError, RuntimeError) as e:`

- [ ] **Step 7: 收窄 _warm_embedding_cache (L313)**

L313: `except Exception as e:` → `except (OSError, ValueError, RuntimeError) as e:`

- [ ] **Step 8: 收窄 _distill_memories_task (L326)**

L326: `except Exception as e:` → `except (OSError, RuntimeError) as e:`

- [ ] **Step 9: 收窄 _refresh_mail_token_task (L350, L355)**

L350: `except Exception:` (clear_auth_cache) → `except (ImportError, AttributeError):`
L355: `except Exception as e:` (mail token refresh) → `except (OSError, RuntimeError, TimeoutError) as e:`

- [ ] **Step 10: 编译验证**

Run: `python -m py_compile core/background_tasks.py`
Expected: 无错误输出

- [ ] **Step 11: 提交**

```bash
git add core/background_tasks.py
git commit -m "fix: 收窄core/background_tasks.py 17处except Exception为具体异常类型"
```

---

### Task 3: 收窄 db/database.py 的 17 处 except Exception

**Files:**
- Modify: `db/database.py` (17处 except Exception)

**Interfaces:**
- Consumes: 无外部依赖
- Produces: 更健壮的数据库操作异常处理

- [ ] **Step 1: 收窄 _detect_fs_type (L43, L54)**

L43: `except Exception:` (Windows FS detect) → `except (OSError, ValueError):`
L54: `except Exception:` (Linux FS detect) → `except (OSError, ValueError):`

- [ ] **Step 2: 收窄 init/close (L77, L85, L106, L118)**

L77: `except Exception:` (close old connection) → `except (OSError, RuntimeError):`
L85: `except Exception as e:` (PRAGMA busy_timeout) → `except (OSError, RuntimeError) as e:`
L106: `except Exception as e:` (PRAGMA 失败) → `except (OSError, RuntimeError) as e:`
L118: `except Exception as e:` (验证 journal_mode) → `except (OSError, RuntimeError) as e:`

- [ ] **Step 3: 收窄 migration 相关 (L141, L189, L243, L253, L265)**

L141: `except Exception as e:` (composite indexes) → `except (OSError, RuntimeError) as e:`
L189: `except Exception:` (migration dirty close) → `except (OSError, RuntimeError):`
L243: `except Exception as e:` (migration execution) → `except (OSError, RuntimeError) as e:`
L253: `except Exception:` (migration dirty record) → `except (OSError, RuntimeError):`
L265: `except Exception:` (migration failure close) → `except (OSError, RuntimeError):`

- [ ] **Step 4: 收窄 FTS5/清理相关 (L895, L905, L925, L1092, L1120, L1127)**

L895: `except Exception as e:` (插入默认清理策略) → `except (OSError, RuntimeError) as e:`
L905: `except Exception:` (FTS5 trigger drop) → `except (OSError, RuntimeError):`
L925: `except Exception as e:` (FTS5 trigger create) → `except (OSError, RuntimeError) as e:`
L1092: `except Exception:` (cleanup config read) → `except (OSError, ValueError):`
L1120: `except Exception as e:` (cleanup failed) → `except (OSError, RuntimeError) as e:`
L1127: `except Exception as e:` (cleanup commit) → `except (OSError, RuntimeError) as e:`

- [ ] **Step 5: 编译验证**

Run: `python -m py_compile db/database.py`
Expected: 无错误输出

- [ ] **Step 6: 提交**

```bash
git add db/database.py
git commit -m "fix: 收窄db/database.py 17处except Exception为具体异常类型"
```

---

### Task 4: 收窄 agent_dispatcher.py 的 15 处 except Exception

**Files:**
- Modify: `agent_dispatcher.py` (15处 except Exception)

**Interfaces:**
- Consumes: 无外部依赖
- Produces: 更健壮的调度器异常处理

- [ ] **Step 1: 收窄 client 关闭/重载 (L218, L237, L351)**

L218: `except Exception:` (close client) → `except (OSError, RuntimeError):`
L237: `except Exception as e:` (reload client) → `except (OSError, ValueError, RuntimeError) as e:`
L351: `except Exception:` (close old client) → `except (OSError, RuntimeError):`

- [ ] **Step 2: 收窄 chat/fallback (L384, L390)**

L384: `except Exception as e:` (chat failed) → `except (RuntimeError, OSError, asyncio.TimeoutError, ValueError) as e:`
L390: `except Exception as e2:` (fallback failed) → `except (RuntimeError, OSError, asyncio.TimeoutError, ValueError) as e2:`

- [ ] **Step 3: 收窄 submit_memory/send_message (L631, L640, L735, L741)**

L631: `except Exception as e:` (submit_memory call) → `except (RuntimeError, OSError, ValueError) as e:`
L640: `except Exception as e:` (send_message call) → `except (RuntimeError, OSError, ValueError) as e:`
L735: `except Exception as ve:` (vec upsert) → `except (OSError, RuntimeError, ValueError) as ve:`
L741: `except Exception as e:` (submit_memory) → `except (RuntimeError, OSError, ValueError) as e:`

- [ ] **Step 4: 收窄 send_message_to_agent (L780)**

L780: `except Exception as e:` → `except (RuntimeError, OSError, ValueError) as e:`

- [ ] **Step 5: 收窄 close/refresh/routing (L862, L909, L982, L1027, L1124)**

L862: `except Exception:` (close sub agent) → `except (OSError, RuntimeError):`
L909: `except Exception as e:` (client refresh) → `except (OSError, ValueError, RuntimeError) as e:`
L982: `except Exception as e:` (routing config load) → `except (OSError, ValueError, RuntimeError) as e:`
L1027: `except Exception as e:` (classify task) → `except (OSError, ValueError, RuntimeError) as e:`
L1124: `except Exception as e:` (routing v2 config) → `except (OSError, ValueError, json.JSONDecodeError) as e:`

- [ ] **Step 6: 编译验证**

Run: `python -m py_compile agent_dispatcher.py`
Expected: 无错误输出

- [ ] **Step 7: 提交**

```bash
git add agent_dispatcher.py
git commit -m "fix: 收窄agent_dispatcher.py 15处except Exception为具体异常类型"
```

---

### Task 5: 添加 trace ID 串联日志和指标

**Files:**
- Create: `core/trace_context.py`
- Modify: `web/server.py` (中间件注入 trace ID)
- Modify: `core/sla_exporter.py` (指标关联 trace ID)
- Modify: `web/ws_hub.py` (WebSocket 消息关联 trace ID)

**Interfaces:**
- Consumes: 标准库 `contextvars`
- Produces: `get_trace_id()` 函数供日志和指标使用

- [ ] **Step 1: 创建 core/trace_context.py**

```python
"""请求级 trace ID — 基于 ContextVar 实现协程隔离。

用法:
    from core.trace_context import get_trace_id, set_trace_id
    set_trace_id("abc123")
    tid = get_trace_id()  # "abc123"
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


def new_trace_id() -> str:
    tid = uuid.uuid4().hex[:16]
    _trace_id.set(tid)
    return tid


def clear_trace_id() -> None:
    _trace_id.set("")
```

- [ ] **Step 2: 在 server.py 中间件注入 trace ID**

在现有 Prometheus 中间件之前，添加 trace ID 注入逻辑：

```python
from core.trace_context import new_trace_id, get_trace_id, clear_trace_id

# 在中间件函数内，请求处理前：
tid = new_trace_id()
response = await call_next(request)
# 响应头添加 trace ID 方便调试
response.headers["X-Trace-Id"] = tid
clear_trace_id()
return response
```

- [ ] **Step 3: 在 ws_hub.py WebSocket 消息处理注入 trace ID**

在 `_handle_chat` 等消息处理函数入口调用 `new_trace_id()`，日志中输出 trace ID：

```python
from core.trace_context import new_trace_id, get_trace_id

# 在消息处理入口：
tid = new_trace_id()
logger.info("ws.chat.start trace_id={} conn_id={}", tid, conn_id)
```

- [ ] **Step 4: 在 sla_exporter.py 指标中关联 trace ID**

在 `inc_request` 和 `observe_latency` 中将 trace ID 作为 label：

```python
def inc_request(self, endpoint: str, status: str) -> None:
    from core.trace_context import get_trace_id
    tid = get_trace_id()
    key = (endpoint, status, tid) if tid else (endpoint, status)
    # ... 现有逻辑
```

注意：Prometheus 指标高基数 label (trace_id) 会导致内存膨胀。改为在 `export()` 时附加注释行而非 label：

```python
# 在 export() 末尾添加：
tid = get_trace_id()
if tid:
    lines.append(f"# trace_id {tid}")
```

- [ ] **Step 5: 编译验证**

Run: `python -m py_compile core/trace_context.py && python -m py_compile web/server.py && python -m py_compile web/ws_hub.py && python -m py_compile core/sla_exporter.py`
Expected: 无错误输出

- [ ] **Step 6: 提交**

```bash
git add core/trace_context.py web/server.py web/ws_hub.py core/sla_exporter.py
git commit -m "feat: 添加trace ID串联日志和指标(ContextVar协程隔离)"
```

---

### Task 6: 固化测试环境依赖

**Files:**
- Modify: `requirements.txt` (确保 pytest-asyncio 等测试依赖明确列出)
- Modify: `pytest.ini` (确保配置正确)
- Modify: `.github/workflows/ci-tests.yml` (使用 requirements.lock 安装)

**Interfaces:**
- Consumes: 现有 requirements.txt
- Produces: 可复现的测试环境

- [ ] **Step 1: 检查并更新 requirements.txt**

确保以下测试依赖明确列出（带版本下限）：
```
pytest>=7.0
pytest-asyncio>=0.21
pytest-cov>=4.0
pytest-timeout>=2.0
```

- [ ] **Step 2: 更新 CI 工作流使用 pip install -r requirements.lock**

在 ci-tests.yml 的 Install dependencies 步骤中：
```yaml
- name: Install dependencies
  run: |
    pip install -r requirements.lock 2>/dev/null || pip install -r requirements.txt
    pip install pytest-asyncio pytest-cov pytest-timeout
```

- [ ] **Step 3: 编译验证**

Run: `python -c "import pytest; import pytest_asyncio; import pytest_cov; print('OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add requirements.txt .github/workflows/ci-tests.yml
git commit -m "fix: 固化测试环境依赖,CI使用requirements.lock"
```

---

### Task 7: 添加 Kubernetes 部署配置

**Files:**
- Create: `deploy/k8s/namespace.yaml`
- Create: `deploy/k8s/deployment.yaml`
- Create: `deploy/k8s/service.yaml`
- Create: `deploy/k8s/configmap.yaml`
- Create: `deploy/k8s/secret.yaml`
- Create: `deploy/k8s/kustomization.yaml`

**Interfaces:**
- Consumes: 现有 Dockerfile
- Produces: 标准 Kubernetes 部署资源

- [ ] **Step 1: 创建 namespace.yaml**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: xiaoda-agent
  labels:
    app: xiaoda-agent
```

- [ ] **Step 2: 创建 configmap.yaml**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: xiaoda-config
  namespace: xiaoda-agent
data:
  QQBOT_APP_ID: ""
  NUDGE_ENABLED: "false"
  QQ_HITL_ENABLED: "true"
  MEMORY_DISTILL_ENABLED: "false"
  LOG_LEVEL: "INFO"
```

- [ ] **Step 3: 创建 secret.yaml (模板)**

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: xiaoda-secrets
  namespace: xiaoda-agent
type: Opaque
stringData:
  QQBOT_APP_SECRET: ""
  OPENAI_API_KEY: ""
  MASTER_QQ_OPENID: ""
```

- [ ] **Step 4: 创建 deployment.yaml**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: xiaoda-agent
  namespace: xiaoda-agent
  labels:
    app: xiaoda-agent
spec:
  replicas: 1
  selector:
    matchLabels:
      app: xiaoda-agent
  template:
    metadata:
      labels:
        app: xiaoda-agent
    spec:
      containers:
        - name: xiaoda-agent
          image: xiaoda-agent:latest
          ports:
            - containerPort: 8000
              name: http
          envFrom:
            - configMapRef:
                name: xiaoda-config
            - secretRef:
                name: xiaoda-secrets
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
          livenessProbe:
            httpGet:
              path: /api/health
              port: http
            initialDelaySeconds: 30
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /api/health
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
          volumeMounts:
            - name: data
              mountPath: /app/data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: xiaoda-data
```

- [ ] **Step 5: 创建 service.yaml**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: xiaoda-agent
  namespace: xiaoda-agent
spec:
  selector:
    app: xiaoda-agent
  ports:
    - port: 80
      targetPort: http
      name: http
  type: ClusterIP
```

- [ ] **Step 6: 创建 kustomization.yaml**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: xiaoda-agent
resources:
  - namespace.yaml
  - configmap.yaml
  - secret.yaml
  - deployment.yaml
  - service.yaml
```

- [ ] **Step 7: 提交**

```bash
git add deploy/k8s/
git commit -m "feat: 添加Kubernetes部署配置(Deployment/Service/ConfigMap/Secret)"
```

---

## 执行顺序

1. Task 1 → Task 2 → Task 3 → Task 4 (异常收窄，按影响范围从大到小)
2. Task 5 (trace ID，依赖 Task 1-4 完成后代码稳定)
3. Task 6 (依赖固化)
4. Task 7 (K8s 配置，独立于代码修改)