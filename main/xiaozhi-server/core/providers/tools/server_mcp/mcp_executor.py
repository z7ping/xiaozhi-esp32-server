"""服务端MCP工具执行器"""

from typing import Dict, Any, Optional
from core.media.mcp_media_player import MCPMediaPlayer, MediaTrack, trusted_hosts, validate_audio_link
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import Action, ActionResponse
from .mcp_manager import ServerMCPManager


class ServerMCPExecutor(ToolExecutor):
    """服务端MCP工具执行器"""

    def __init__(self, conn):
        self.conn = conn
        self.mcp_manager: Optional[ServerMCPManager] = None
        self._initialized = False

    async def initialize(self):
        """初始化MCP管理器"""
        if not self._initialized:
            self.mcp_manager = ServerMCPManager(self.conn)
            self._initialized = True
            await self.mcp_manager.initialize_servers()

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行服务端MCP工具"""
        if not self._initialized or not self.mcp_manager:
            return ActionResponse(
                action=Action.ERROR,
                response="MCP管理器未初始化",
            )

        try:
            # 移除mcp_前缀（如果有）
            actual_tool_name = tool_name
            if tool_name.startswith("mcp_"):
                actual_tool_name = tool_name[4:]

            if actual_tool_name == "control_mcp_media_playback":
                player = getattr(conn, "mcp_media_player", None)
                status = await player.control(arguments.get("action")) if player else "当前没有播放内容"
                return ActionResponse(action=Action.RECORD, result=status, response=status)

            result = await self.mcp_manager.execute_tool(actual_tool_name, arguments)

            if actual_tool_name == "jellyfin_audio":
                if getattr(result, "isError", False):
                    return ActionResponse(action=Action.ERROR, response="媒体服务返回错误，请检查 Jellyfin MCP")
                contents = getattr(result, "content", []) or []
                links = [item for item in contents if getattr(item, "type", None) == "resource_link"]
                if not links:
                    return ActionResponse(action=Action.ERROR, response="MCP 未返回可播放的音频资源")
                allowed = trusted_hosts()
                if not allowed:
                    return ActionResponse(action=Action.ERROR, response="请先配置 MCP_MEDIA_ALLOWED_HOSTS")
                # Validate all links before starting any network request.
                if len(links) > 1000:
                    return ActionResponse(action=Action.ERROR, response="歌单最多支持 1000 首歌曲")
                tracks = []
                for item in links:
                    url = validate_audio_link(item.uri, item.mimeType, allowed)
                    tracks.append(MediaTrack(title=(item.name or "Jellyfin 音频")[:100], url=url))
                player = getattr(conn, "mcp_media_player", None)
                if player is None:
                    player = MCPMediaPlayer(conn)
                    conn.mcp_media_player = player
                count = await player.replace(tracks)
                # Never stringify MCP result: ResourceLinks contain bearer tokens.
                message = f"已开始播放 {count} 首歌曲"
                return ActionResponse(action=Action.RECORD, result=message, response=message)

            return ActionResponse(action=Action.REQLLM, result=str(result))

        except ValueError as e:
            return ActionResponse(
                action=Action.NOTFOUND,
                response=str(e),
            )
        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                response=str(e),
            )

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有服务端MCP工具"""
        if not self._initialized or not self.mcp_manager:
            return {}

        tools = {}
        mcp_tools = self.mcp_manager.get_all_tools()

        # Advertise playback controls only when Jellyfin's audio tool is available.
        if any(t.get("function", {}).get("name") == "jellyfin_audio" for t in mcp_tools):
            control_name = "control_mcp_media_playback"
            control_desc = {
                "type": "function",
                "function": {
                    "name": control_name,
                    "description": "控制当前 ESP32 设备上已播放的 Jellyfin 音乐：暂停、继续、上一首、下一首、停止。搜索新歌或播放新歌单请调用 jellyfin_search/jellyfin_playlists 和 jellyfin_audio。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["pause", "resume", "next", "previous", "stop"],
                                "description": "当前播放队列操作",
                            }
                        },
                        "required": ["action"],
                    },
                },
            }
            tools[control_name] = ToolDefinition(
                name=control_name, description=control_desc, tool_type=ToolType.SERVER_MCP
            )

        for tool in mcp_tools:
            func_def = tool.get("function", {})
            tool_name = func_def.get("name", "")
            if tool_name == "":
                continue
            tools[tool_name] = ToolDefinition(
                name=tool_name, description=tool, tool_type=ToolType.SERVER_MCP
            )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的服务端MCP工具"""
        if not self._initialized or not self.mcp_manager:
            return False

        # 移除mcp_前缀（如果有）
        actual_tool_name = tool_name
        if tool_name.startswith("mcp_"):
            actual_tool_name = tool_name[4:]

        if actual_tool_name == "control_mcp_media_playback":
            return self.mcp_manager.is_mcp_tool("jellyfin_audio")
        return self.mcp_manager.is_mcp_tool(actual_tool_name)

    async def cleanup(self):
        """清理MCP连接"""
        if self.mcp_manager:
            await self.mcp_manager.cleanup_all()
