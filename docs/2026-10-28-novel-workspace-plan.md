# 星瑶小说工作区实施计划（P1 + P2）

- 设计文档：`docs/2026-10-28-novel-workspace-design.md`
- 日期：2026-10-28
- 验收口径：每项以对应测试命令退出码 0 为准，未跑过不算通过

## 任务分解

| # | 任务 | 产出文件 | 依赖 | 验证命令 |
|---|---|---|---|---|
| 1 | 服务骨架 + `novel_init` + Agent 转换器 | `core/novel_service.py`、`plugins/novel_workspace/*`、`tests/test_novel_service.py` | 无 | `python -m pytest tests/test_novel_service.py -q` |
| 2 | `novel_status` + `novel_wordcount` | 同上扩展 | 1 | `python -m pytest tests/test_novel_service.py -q -k "status or wordcount"` |
| 3 | `novel_track` + `novel_outline` + vendor 脚本 | `plugins/novel_workspace/vendor/*` | 1 | `python -m pytest tests/test_novel_service.py -q -k "track or outline"` |
| 4 | `novel_audit` 确定性检查 | `core/novel_service.py` 扩展 | 1,3 | `python -m pytest tests/test_novel_service.py -q -k "audit"` |
| 5 | 端到端 3 章验证 | `tests/test_novel_e2e.py` | 2,3,4 | `python -m pytest tests/test_novel_e2e.py -q` |
| 6 | ApiBridge 接口 | `gui_bridge.py`、`tests/test_novel_bridge.py` | 2,4 | `python -m pytest tests/test_novel_bridge.py -q` |
| 7 | NovelPane + 页签注册 | `frontend/src/components/NovelPane.vue` 等 | 6 | `npm --prefix frontend run test -- NovelPane` |
| 8 | 全量回归 | 无新增 | 5,7 | `python -m pytest -q -m "not slow"` |

## 执行约束

- 每项开始前先读设计文档对应章节，不凭记忆实现。
- 新增测试必须能在无网络、无 Node 的环境下通过（Node 缺失走 `skipped`）。
- 不改动上游 oh-story 技能包本体；所有适配在星瑶侧。
- 不修改 `core/subagent_registry.py`；Agent 转换在服务层完成。
- 前端新增页签不破坏现有 6 个页签的持久化与切换逻辑。

## 回滚点

- 任务 1 完成即可独立提交（服务骨架 + 建书能力）。
- 任务 3 完成后可单独验收"追踪事务不覆盖"。
- 任务 7 的页签注册单独提交，便于单独回退。
