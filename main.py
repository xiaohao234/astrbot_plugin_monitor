"""
astrbot_plugin_monitor
======================

两个功能（均通过关键词触发，无需 / 前缀、无需 @，不与其它机器人的 / 指令冲突）：
1. 系统信息查询：发送配置的触发词（默认「sysinfo」或「系统信息」），
   仅允许配置的管理员 QQ 查看。
   - 系统运行时间、系统版本、程序运行时长、CPU/内存/磁盘占用率、系统状况分析。
   - 系统版本自动检测真实值，跨平台支持 Windows / Linux（各发行版）/ macOS。
   - 非管理员查询时返回友好提示，避免尴尬。
   - 输出不含分隔线与表情，只输出信息（每项一行）。
2. ping 测试：发送「ping」，回复「测试完成，用时xxxms」，用时为机器人内部回复延迟。

所有个性化内容均可在 AstrBot 管理面板的插件配置中修改（见 _conf_schema.json），
代码中不含任何隐私信息。

依赖：psutil（AstrBot 自带，无需额外安装）。
"""

import asyncio
import os
import platform
import sys
import time

import psutil

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 插件配置缺失/为空时的内置兜底默认值
DEFAULT_TRIGGERS = ["sysinfo", "系统信息"]
DEFAULT_NO_PERM_REPLY = "调用系统 API 失败，请稍后再试～"


@register("astrbot_plugin_monitor", "xiaohao234", "系统信息查询与 ping 测试插件", "1.3.0")
class MonitorPlugin(Star):
    """系统信息查询与 ping 测试插件。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 触发词统一转小写，做整条消息的精确匹配（不区分大小写）
        raw_triggers = self.config.get("sysinfo_triggers") or DEFAULT_TRIGGERS
        self._triggers = {str(t).strip().lower() for t in raw_triggers if str(t).strip()}

        # 无权限回复
        self._no_perm_reply = str(
            self.config.get("no_permission_reply") or DEFAULT_NO_PERM_REPLY
        ).strip() or DEFAULT_NO_PERM_REPLY

        # 允许查询系统信息的管理员 QQ
        self._owner_qq = str(self.config.get("owner_qq") or "").strip()

        # 磁盘占用率统计路径：未配置时按平台自动选择
        self._disk_path = (
            str(self.config.get("disk_path") or "").strip()
            or ("C:\\" if os.name == "nt" else "/")
        )

    async def initialize(self):
        """插件实例化后调用。预热 CPU 采样，并校验关键配置。"""
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            logger.warning("[monitor] 初始化 CPU 采样失败，不影响插件运行。")
        if not self._owner_qq:
            logger.warning(
                "[monitor] 未配置管理员 QQ（owner_qq），当前所有人查询系统信息都会被拒绝，"
                "请在插件配置中设置。"
            )
        if not self._triggers:
            logger.warning("[monitor] 触发词列表为空，系统信息功能将无法触发，已回退默认值。")
            self._triggers = {t.lower() for t in DEFAULT_TRIGGERS}

    # ============================================================
    # 系统信息查询（关键词触发，无需 / 前缀）
    # ============================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def sysinfo(self, event: AstrMessageEvent):
        """发送配置的触发词即可查询（无需 / 前缀，无需 @）。"""
        text = event.message_str.strip().lower()
        if text not in self._triggers:
            return  # 不是查询关键词，正常放行，不干预其它流程
        # 命中关键词：抑制默认 LLM，避免 @机器人 时 AI 也跟着回复
        event.should_call_llm(False)
        sender_id = event.get_sender_id()
        if sender_id != self._owner_qq:
            # 非管理员：友好回复，不暴露权限逻辑
            yield event.plain_result(self._no_perm_reply)
            return
        try:
            report = await self._collect_sysinfo()
            yield event.plain_result(report)
        except Exception as e:
            # 采集异常时把错误发到会话，而不是让进程崩溃
            logger.exception("采集系统信息失败")
            yield event.plain_result(f"采集系统信息失败：{e}")

    async def _collect_sysinfo(self) -> str:
        """采集并拼接系统信息。阻塞调用（CPU 采样/文件读取）放到线程里执行。"""
        # 系统已开机时长
        uptime_str = self._format_duration(time.time() - psutil.boot_time())

        # 系统版本（自动检测真实值；读 /etc/os-release 属于阻塞 IO，放线程）
        os_version = await asyncio.to_thread(self._get_os_version)

        # 当前程序（AstrBot 进程）已运行时长
        proc_create_time = psutil.Process(os.getpid()).create_time()
        proc_uptime_str = self._format_duration(time.time() - proc_create_time)

        # CPU 占用率（interval 采样会阻塞，放到工作线程）
        cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 0.3)

        # 内存占用率
        mem_percent = psutil.virtual_memory().percent

        # 磁盘占用率（stat 系统调用放线程）
        disk_percent = (await asyncio.to_thread(psutil.disk_usage, self._disk_path)).percent

        # 系统状况分析
        status = self._analyze_health(cpu_percent, mem_percent, disk_percent)

        # 只输出信息，不加分隔线和表情
        lines = [
            f"系统运行时间：{uptime_str}",
            f"系统版本：{os_version}",
            f"程序运行时长：{proc_uptime_str}",
            f"CPU占用率：{cpu_percent:.1f}%",
            f"内存占用率：{mem_percent:.1f}%",
            f"磁盘占用率：{disk_percent:.1f}%",
            status,
        ]
        return "\n".join(lines)

    @staticmethod
    def _get_os_version() -> str:
        """跨平台获取真实的操作系统版本。"""
        system = platform.system()
        if system == "Linux":
            # 主流发行版均遵循 freedesktop 的 os-release 规范
            try:
                with open("/etc/os-release", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            return line.partition("=")[2].strip().strip("\"'")
            except OSError:
                pass
            return f"Linux {platform.release()}"  # 兜底：无 os-release 的精简系统
        if system == "Windows":
            release = platform.release()
            # 部分版本 Python 在 Win11 上可能误报为 10，按 build 号修正
            try:
                if release == "10" and sys.getwindowsversion().build >= 22000:
                    release = "11"
            except (AttributeError, ValueError):
                pass
            return f"Windows {release}"
        if system == "Darwin":
            ver = platform.mac_ver()[0]
            return f"macOS {ver}" if ver else "macOS"
        return platform.platform() or system

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """把秒数格式化为「X天Y小时Z分钟」之类的人话。"""
        total = max(0, int(seconds))
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        if days > 0:
            return f"{days}天{hours}小时{minutes}分钟"
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        if minutes > 0:
            return f"{minutes}分钟{secs}秒"
        return f"{secs}秒"

    @staticmethod
    def _analyze_health(cpu: float, mem: float, disk: float) -> str:
        """根据 CPU/内存/磁盘占用率给出系统状况结论（纯文本，无表情）。"""
        metrics = {"CPU": cpu, "内存": mem, "磁盘": disk}
        bottleneck = max(metrics, key=metrics.get)
        max_val = metrics[bottleneck]
        if max_val < 70:
            return "系统状况：健康"
        if max_val < 85:
            return f"系统状况：良好（{bottleneck}占用 {max_val:.1f}%）"
        if max_val < 90:
            return f"系统状况：注意（{bottleneck}占用 {max_val:.1f}%）"
        return f"系统状况：紧张（{bottleneck}占用 {max_val:.1f}%）"

    # ============================================================
    # ping 测试（关键词触发，无需 / 前缀）
    # ============================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def ping(self, event: AstrMessageEvent):
        """发送 ping，回复测试完成及机器人内部回复耗时（ms）。"""
        text = event.message_str.strip().lower()
        if text != "ping":
            return  # 不是 ping，正常放行
        # 平台时间戳只有秒级整精度，无法做亚秒级计时；
        # 这里用高精度单调时钟测量机器人内部回复延迟。
        start = time.perf_counter()
        event.should_call_llm(False)  # 抑制默认 LLM，避免 AI 也跟着回复
        elapsed_ms = (time.perf_counter() - start) * 1000  # 转为毫秒
        # 不足 0.01 毫秒时显示下限，避免显示 0.00
        if elapsed_ms < 0.01:
            elapsed_str = "<0.01"
        else:
            elapsed_str = f"{elapsed_ms:.2f}"
        yield event.plain_result(f"测试完成，用时{elapsed_str}ms")
