"""Media plugin — ffprobe metadata probing for uploaded audio/video files."""

from courtier_plugin_sdk import PluginRuntime
from tools import ProbeMediaTool, TranscodeVideoTool


class MediaPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [],
            "system_prompt": "# 媒体探测\n\n## 能力\n对上传的音频/视频文件做 ffprobe 元数据探测（格式/时长/分辨率/编码），并提供非 mp4 视频的 mp4 转码服务。\n",
        }

    def _setup_handlers(self):
        self.register_tool(ProbeMediaTool())
        self.register_tool(TranscodeVideoTool())


if __name__ == "__main__":
    import asyncio

    asyncio.run(MediaPlugin().run())
