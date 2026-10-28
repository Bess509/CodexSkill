"""兼容上游 Anthropic 网关的请求体补丁。

背景
----
LiteLLM 把 OpenAI 格式的函数工具转成 Anthropic 格式时，会给每个工具硬编码
``type="custom"``（litellm/llms/anthropic/chat/transformation.py:789、:795）。
Anthropic 官方接口接受该字段，但部分第三方 Anthropic 兼容网关反序列化时
只认 ``web_search_*`` 一类服务端工具类型，遇到 ``custom`` 直接 422：

    tools[0]: unknown variant `custom`, expected `web_search_20250305`
    or `web_search_20260209`

同一条请求把 ``type`` 去掉就完全正常——函数工具本来就可以省略该字段
（Anthropic 缺省即视为 custom）。因此这里在工具映射完成后统一删掉它，
对 MiniMax / GLM 这类本来就容忍该字段的模型没有副作用。

用法
----
被 ``bridge_guard`` 在模块导入时调用（``compat_patch.apply()``）。代理启动时会
加载 ``bridge_guard.proxy_handler_instance`` 回调，因此补丁随代理一起生效。
"""
from __future__ import annotations

_APPLIED = False


def apply() -> bool:
    """给 AnthropicConfig._map_tools 打补丁，返回是否本次真正执行了打补丁。"""
    global _APPLIED
    if _APPLIED:
        return False

    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    original = AnthropicConfig._map_tools

    def _map_tools(self, tools):
        anthropic_tools, mcp_servers = original(self, tools)
        try:
            for tool in anthropic_tools:
                # 只动函数工具；web_search / computer_* / mcp 等服务端工具必须保留类型
                if isinstance(tool, dict) and tool.get("type") == "custom":
                    tool.pop("type", None)
        except Exception as exc:  # noqa: BLE001  补丁失败不能拖垮主链路
            print(f"[compat_patch] 清理 type 字段失败，已放行原始请求: {exc}")
        return anthropic_tools, mcp_servers

    AnthropicConfig._map_tools = _map_tools
    _APPLIED = True
    print("[compat_patch] 已移除函数工具的 type='custom'，兼容严格网关")
    return True
