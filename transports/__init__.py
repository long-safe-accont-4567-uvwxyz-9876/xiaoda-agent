"""Agnes 共享 HTTP client 基础设施。

历史上的双 transport 栈（ProviderTransport ABC + MiMo/Agnes Transport 类）
已移除；本包仅剩 agnes_transport.py 的共享 httpx client 配置，被
llm_gateway/client_lifecycle、llm_gateway/router_metrics、model_router 与
web/server 引用。新 transport 一律走 llm_gateway/transports/ 体系。
"""
