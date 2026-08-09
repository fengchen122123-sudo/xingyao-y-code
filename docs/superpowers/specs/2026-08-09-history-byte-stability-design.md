# 历史消息字节稳定性设计

## 目标

消除普通请求准备阶段对数据库历史的隐式重写，并确保视觉副模型发给主模型的 canonical 文本与数据库下一轮重建出的消息逐字节一致。

## 现状根因

- `prepare_turn_messages` 每轮无条件调用 `prune_history_files`。即使函数多数时候不改数据，请求准备仍同时承担读历史和写历史职责，无法证明请求路径只读。
- 非视觉主模型遇到图片时，`_describe_images_with_secondary` 把最新 user 消息原地替换为 `[图片分析结果]`，但数据库仍保存原始媒体。下一轮从 DB 重建后会得到另一份占位文本，公共前缀从该消息起失效。

## 设计

### 历史文件维护移到用户消息入库边界

`MemoryManager.add_message` 在成功写入顶层 user 消息后调用 `prune_history_files`。批量 `add_messages` 不触发该维护，因为它也用于工具结果和后台通知持久化，不能在 provider 工具循环中改写旧历史。

`prepare_turn_messages` 删除无条件 prune。只有超过 95% 上下文水位且摘要后仍超限的显式压缩分支可以调用 `prune_history_files(max_turns=1)`；该分支本来就是一次明确的 context epoch 重置。

### 视觉 canonical 消息使用 CAS 持久化

新增：

```python
MemoryManager.replace_latest_user_message_content(
    session_name,
    expected_content,
    canonical_content,
) -> bool
```

方法在一个事务内读取最后一条 user 消息，按现有序列化规则比较 `expected_content`，完全相等才更新为 `canonical_content` 并 commit。若会话/消息不存在或内容已变化，返回 False，不覆盖并发的新输入。

`_chat_with_model_generator` 在调用视觉副模型前保存最新 user 原始 content；描述成功并改变消息后，如果本轮启用持久化且有 `parent_session`，必须执行 CAS。CAS 返回 False 视为状态分叉，立即抛错，不带着无法在下一轮复现的 prompt 继续请求。`persist=False` 的子代理/测试不写 DB。

主模型原生支持图片时 `_describe_images_with_secondary` 不改消息，因此不触发 CAS。旧媒体的确定性折叠规则保持现状。

## 不变量

- 普通 `prepare_turn_messages` 读取历史，不调用文件历史维护。
- 顶层 user 消息入库完成后，旧文件折叠在任何请求构建之前完成。
- 视觉 canonical 文本成功发送给主模型前，DB 必须已经保存完全相同的 content。
- CAS 不允许覆盖更新后的最后 user 消息。
- 不修改消息顺序、tool_call 配对或 system 稳定前缀。

## 测试

1. `prepare_turn_messages` 在低水位不调用 `prune_history_files`。
2. `add_message(role=user)` 调用一次维护；`add_messages` 写 assistant/tool/user 通知批次均不调用。
3. CAS 对 dict/list/string 使用与入库一致的 UTF-8 JSON 序列化；成功后 `get_history` 返回 canonical content。
4. expected 不匹配时返回 False 且 DB 原文不变。
5. 非视觉主模型图片描述后，provider 捕获的 user content 与 DB `get_history` 完全相等。
6. `persist=False` 不尝试 CAS。
7. 相关测试、全量 pytest、py_compile、前端测试与构建通过。
