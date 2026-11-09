"""RealUIAHost：真实 COM 线程 UIA 元素收集与语义操作（2026-09-06 阶段二）。

复用 windows_surfaces._ComThread（专用 COM 线程 + 有界命令队列）与
UIAutomationCore CLSID；UIA 元素对象绝不离开 COM 线程，跨线程只传
脱敏 dict（ref/kind/name/bounds/enabled）。所有失败归一为稳定错误码：
- collect 失败 → computer_backend_error
- ref 不存在/已过期 → computer_element_stale
- 元素不支持 Invoke/Value pattern → computer_element_unsupported
"""

from __future__ import annotations

import logging

from core.computer.models import ComputerControlError
from core.computer.platforms.windows_elements import MAX_ELEMENTS, MAX_TREE_DEPTH

_LOGGER = logging.getLogger(__name__)

# UIA 稳定常量（UIAutomationCore 规范固定）。
_UIA_CONTROL_TYPE_PROPERTY_ID = 30003
_UIA_INVOKE_PATTERN_ID = 10000
_UIA_VALUE_PATTERN_ID = 10002
_CUIAUTOMATION_CLSID = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"

# 名称/值摘要截断（collect 与 probe 共用同一口径）。
_MAX_NAME_CHARS = 80
# probe 返回的匹配摘要字段上限：只做「命中证据」，不承载可操作引用。
_PROBE_SUMMARY_CHARS = 120

# 只收集的可交互控件类型 ID（UIA ControlType IDs，规范固定）。
_INTERACTIVE_CONTROL_TYPE_IDS = frozenset(
    {
        50000,  # Button
        50002,  # CheckBox
        50003,  # ComboBox
        50004,  # Edit
        50005,  # Hyperlink
        50007,  # ListItem
        50008,  # List（微信聊天消息列表等容器，读屏需要）
        50011,  # MenuItem
        50013,  # RadioButton
        50015,  # Slider
        50019,  # TabItem
        50024,  # TreeItem
        50029,  # DataItem
        50030,  # Document
        50031,  # SplitButton
    }
)


class RealUIAHost:
    """真实 UIA 主机：全部 UIA 访问在专属 COM 线程执行。"""

    def __init__(self) -> None:
        from core.computer.platforms.windows_surfaces import _ComThread

        self._com = _ComThread()
        # ref → IUIAutomationElement 映射：只在 COM 线程的 job 内读写
        # （UIA 对象绝不离开该线程；COM 线程单消费者，无并发访问）。
        self._elements: dict[str, object] = {}
        self._com.start()

    # ---------------- 对外三个入口（线程安全经 _ComThread marshal） ----------------

    def collect(self, hwnd: int):
        """ElementFromHandle → ControlView BFS 收集。

        返回 (listing, ref→None 字典)；真实 ref→IUIAutomationElement 映射只
        留在本对象内存（COM 线程内读写，见 _require），供后续 click/set_value
        经 marshal 使用。返回字典仅承载 ref 全集供 locator 做成员门禁——
        COM 对象绝不随返回值离开线程（跨 apartment 传出/释放是未定义行为）。
        """

        def job(uia):
            if uia is None:
                raise ComputerControlError(
                    "computer_backend_error", "UI Automation 未初始化"
                )
            try:
                root = uia.ElementFromHandle(int(hwnd))
            except Exception as error:
                raise ComputerControlError(
                    "computer_backend_error", f"读取前台窗口控件树失败：{error}"
                ) from error
            walker = uia.ControlViewWalker
            listing: list[dict] = []
            elements: dict[str, object] = {}
            counter = 0
            queue = [(root, 0)]
            while queue and counter < MAX_ELEMENTS:
                node, depth = queue.pop(0)
                try:
                    child = walker.GetFirstChildElement(node)
                except Exception:
                    child = None
                while child is not None:
                    if depth + 1 <= MAX_TREE_DEPTH:
                        queue.append((child, depth + 1))
                    if counter < MAX_ELEMENTS:
                        try:
                            type_id = child.CurrentControlType
                        except Exception:
                            type_id = 0
                        if type_id in _INTERACTIVE_CONTROL_TYPE_IDS:
                            counter += 1
                            ref = f"el-{counter}"
                            try:
                                name = (child.CurrentName or "")[:_MAX_NAME_CHARS]
                            except Exception:
                                name = ""
                            try:
                                rect = child.CurrentBoundingRectangle
                                bounds = [
                                    int(rect.left), int(rect.top),
                                    int(rect.right), int(rect.bottom),
                                ]
                            except Exception:
                                bounds = [0, 0, 0, 0]
                            try:
                                enabled = bool(child.CurrentIsEnabled)
                            except Exception:
                                enabled = True
                            listing.append({
                                "ref": ref,
                                "kind": _control_type_name(uia, type_id),
                                "name": name,
                                "bounds": bounds,
                                "enabled": enabled,
                            })
                            elements[ref] = child
                    try:
                        child = walker.GetNextSiblingElement(child)
                    except Exception:
                        child = None
            # 映射替换在 COM 线程内完成：旧 ref 即刻过期（引用在本线程释放）。
            self._elements = elements
            return listing, {ref: None for ref in elements}

        return self._com.call(job)

    def clear(self) -> None:
        """在 COM 线程内清空 ref→元素映射（画面代数变化时释放引用）。"""
        def job(_uia):
            self._elements = {}

        self._com.call(job)

    def probe(self, hwnd: int, matcher: dict) -> dict | None:
        """只读条件探测（wait_for 轮询专用，2026-11-09 Windows-MCP 对齐）。

        硬约束：collect() 会整体替换 ``self._elements``（见本文件 collect 末尾
        ``self._elements = elements``），模型手里的 el-N 会在等待期间全部失效。
        因此 probe **绝不调用 collect、绝不写 self._elements、绝不重新编号**：

        - element_exists / element_enabled：从既有 ``self._elements`` 只读取值
          （CurrentName / CurrentIsEnabled），ref 不存在抛 computer_element_stale
          （引用已过期，等多久都不会变有效，必须让模型重新 list_elements）；
        - text_exists：在 COM 线程内用 ControlViewWalker BFS 匹配 CurrentName
          或 ValuePattern 当前值（大小写不敏感子串），深度上限 MAX_TREE_DEPTH、
          节点上限 MAX_ELEMENTS，单节点 COM 异常跳过该节点。

        返回脱敏匹配摘要（condition/kind/name/value），**不携带 ref**——摘要与
        ref 生命周期解耦，命中后模型继续用原有 el-N 操作或在需要新 ref 时
        list_elements。未命中返回 None。
        """
        payload = dict(matcher or {})
        return self._com.call(lambda uia: self._probe_job(uia, int(hwnd), payload))

    def click(self, ref: str) -> None:
        def job(uia):
            element = self._require(ref)
            try:
                import comtypes.client

                pattern = element.GetCurrentPattern(_UIA_INVOKE_PATTERN_ID)
                if pattern is None:
                    raise ComputerControlError(
                        "computer_element_unsupported",
                        f"元素 {ref} 不支持 Invoke（请改用坐标点击）",
                    )
                invoke = pattern.QueryInterface(
                    comtypes.gen.UIAutomationClient.IUIAutomationInvokePattern
                )
                invoke.Invoke()
            except ComputerControlError:
                raise
            except Exception as error:
                raise ComputerControlError(
                    "computer_backend_error", f"元素 {ref} 点击失败：{error}"
                ) from error

        self._com.call(job)

    def set_value(self, ref: str, text: str) -> None:
        def job(uia):
            element = self._require(ref)
            try:
                import comtypes.client

                pattern = element.GetCurrentPattern(_UIA_VALUE_PATTERN_ID)
                if pattern is None:
                    raise ComputerControlError(
                        "computer_element_unsupported",
                        f"元素 {ref} 不支持 Value 写入（请改用剪贴板粘贴路径）",
                    )
                value = pattern.QueryInterface(
                    comtypes.gen.UIAutomationClient.IUIAutomationValuePattern
                )
                value.SetValue(str(text))
            except ComputerControlError:
                raise
            except Exception as error:
                raise ComputerControlError(
                    "computer_backend_error", f"元素 {ref} 写值失败：{error}"
                ) from error

        self._com.call(job)

    def close(self) -> None:
        try:
            self._com.close()
        except Exception:
            _LOGGER.exception("关闭 UIA COM 线程失败")

    # ---------------- 内部 ----------------

    def _probe_job(self, uia, hwnd: int, matcher: dict) -> dict | None:
        """COM 线程内的 probe 实现（只读；不触碰 self._elements 的赋值）。"""
        condition = str(matcher.get("condition") or "")
        if condition in ("element_exists", "element_enabled"):
            return self._probe_element(condition, matcher.get("element_ref"))
        if uia is None:
            raise ComputerControlError(
                "computer_backend_error", "UI Automation 未初始化"
            )
        return self._probe_text(uia, hwnd, str(matcher.get("text") or ""))

    def _probe_element(self, condition: str, element_ref: object) -> dict | None:
        """element_* 条件：从既有映射按 ref 只读取值，不重建映射、不改编号。"""
        ref = str(element_ref or "")
        element = self._elements.get(ref)
        if element is None:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )
        try:
            name = str(element.CurrentName or "")[:_MAX_NAME_CHARS]
            if condition == "element_enabled" and not bool(element.CurrentIsEnabled):
                return None
        except Exception:
            # 元素随窗口消失或单节点 COM 异常：本次不命中，继续轮询到超时。
            return None
        return {"condition": condition, "ref": ref, "name": name}

    def _probe_text(self, uia, hwnd: int, needle: str) -> dict | None:
        """text_exists 条件：ControlViewWalker BFS 匹配名称或 Value 值。"""
        if not needle:
            return None
        folded = needle.casefold()
        try:
            root = uia.ElementFromHandle(int(hwnd))
        except Exception:
            # 句柄失效/窗口已关闭：本次不命中，交由上层继续轮询。
            return None
        walker = uia.ControlViewWalker
        queue: list[tuple[object, int]] = [(root, 0)]
        visited = 0
        while queue and visited < MAX_ELEMENTS:
            node, depth = queue.pop(0)
            visited += 1
            if depth > 0:
                # 根节点自身名称是窗口标题，不算「界面文本」；从其子节点起匹配。
                name, value = _probe_node_texts(node)
                if folded in name.casefold() or folded in value.casefold():
                    return {
                        "condition": "text_exists",
                        "kind": _probe_node_kind(uia, node),
                        "name": name[:_PROBE_SUMMARY_CHARS],
                        "value": value[:_PROBE_SUMMARY_CHARS],
                    }
            if depth >= MAX_TREE_DEPTH:
                continue
            try:
                child = walker.GetFirstChildElement(node)
            except Exception:
                child = None
            while child is not None:
                if visited + len(queue) < MAX_ELEMENTS:
                    queue.append((child, depth + 1))
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    child = None
        return None

    def _require(self, ref: str):
        """COM 线程内校验 ref 并取回元素；不存在即过期。"""
        element = self._elements.get(str(ref))
        if element is None:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )
        return element


def _probe_node_texts(node) -> tuple[str, str]:
    """单节点的 (CurrentName, ValuePattern 当前值)；任一读取失败按空串处理。

    单节点 COM 异常绝不上抛：个别控件在树变化瞬间读属性会抛，跳过即可，
    不能因为一个坏节点让整次等待失败。
    """
    name = ""
    try:
        name = str(node.CurrentName or "")
    except Exception:
        name = ""
    value = ""
    try:
        import comtypes.client

        pattern = node.GetCurrentPattern(_UIA_VALUE_PATTERN_ID)
        if pattern is not None:
            interface = comtypes.gen.UIAutomationClient.IUIAutomationValuePattern
            value = str(pattern.QueryInterface(interface).CurrentValue or "")
    except Exception:
        value = ""
    return name, value


def _probe_node_kind(uia, node) -> str:
    """匹配节点的控件类型英文名；读取失败返回空串。"""
    try:
        return _control_type_name(uia, node.CurrentControlType)
    except Exception:
        return ""


def _control_type_name(uia, type_id: int) -> str:
    """控件类型 ID → 稳定英文名（ButtonControl 等）；未知回空串。"""
    try:
        programmatic = uia.GetProgrammaticName(type_id) or ""
    except Exception:
        return ""
    if programmatic.startswith("ControlType."):
        return programmatic[len("ControlType."):] + "Control"
    return programmatic
