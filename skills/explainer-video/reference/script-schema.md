# script.json 字段规范与版式说明

分镜脚本是整条流水线的唯一输入：`make_voice.py` 读它的 `narration`，`render_video.py` 读它的 `visual`。

## 顶层结构

```json
{
  "title": "视频标题（只做标识，不上屏）",
  "size": [720, 1280],
  "fps": 30,
  "scenes": [ ... ]
}
```

`size` 会被 `verify_video.py` 用来核对成品分辨率，必须与实际渲染的 `W, H` 一致（渲染器默认 720×1280，改横屏要同时改渲染器顶部常量）。

## 每个 scene

```json
{
  "id": 1,
  "narration": "旁白文本，20~70 字",
  "visual": {
    "type": "title",
    "heading": "画面主标题，≤12 字",
    "lines": ["要点一", "要点二"],
    "meta": {"accent": "blue", "step_no": 1}
  }
}
```

- `id`：必须与 `timeline.json` 中的 `id` 一一对应（配音脚本按 id 命名音频文件）。
- `narration`：会被 TTS 朗读，同时自动换行成底部字幕。
- `heading`：自动换行，超过 2 行会先缩字号、仍超则截断到 2 行。控制在 12 字内最稳。
- `meta.accent`：`blue` `purple` `teal` `amber` 四选一，决定该段的强调色。
- `meta.step_no`：仅 `type=steps` 用，显示在圆形徽章里。

## 九种版式

| type | lines 怎么填 | 渲染效果 |
|---|---|---|
| `title` | `[大标题, 副标题, "词A / 词B / 词C"]` | 居中大标题 64px + 副标题 30px + 第三行按 `/` 拆成胶囊标签（可省） |
| `bullets` | 每项一条要点，≤18 字 | 卡片内逐条出现，行前带 accent 方块 |
| `terms` | 每项 `词名：一句解释`（中文或英文冒号都认） | 最多 3 张小卡，词名 34px accent 色 + 解释 26px |
| `compare` | **成对**：`[左标题, 左说明, 右标题, 右说明]` | 上下两组，中间虚线分隔，最多 4 条（2 组） |
| `steps` | `[步骤标题, 要点1, 要点2, ...]` | 左上角圆形徽章显示 `step_no`，标题 40px，要点逐条出现 |
| `tip` | 每项一条，≤18 字 | 左侧 accent 竖条 + 固定"提示"标签 |
| `table` | 每项 `左列：右列` | 两列表格，隔行底色；**右列超过 12 字会被截断加省略号** |
| `quote` | `[大字引言, 补充说明, ...]` | 第一行 44px 大字居中 + 巨大引号装饰，其余行小字 |
| `outro` | 每项一条，最多显示 3 条 | 居中大标题 + 三行信息 + accent 柔光 |

`terms` / `table` 用冒号拆左右；没有冒号时整行当左列、右列为空（`table` 会显示空右列，所以务必写冒号）。

## 旁白写作约束（TTS 友好）

- 口语、第二人称，像真人讲解；不要"本文将介绍""综上所述"这类书面语。
- **禁止**：emoji、括号注释、markdown 符号、网址原文（TTS 读不出来，字幕也会很丑）。
- 英文词和缩写保留原样（Agent、API、API Key、sk、Token）。
- 每段是完整可独立朗读的句子；段间靠语义衔接，不要每段都用同一个开头词。
- 事实数字不能为了顺口改掉。

## 字数与时长

中文语速（`zh-CN-XiaoxiaoNeural` + `+5%`）实测 **≈ 4.5 字/秒**。

```
音频总长 ≈ 旁白字数 ÷ 4.5
成片时长 = 音频总长 + gap × (分镜数 - 1) + 尾部留白
```

写脚本时先按目标时长反推总字数，写完用脚本数一遍（去掉标点空格后的字符数），超了就删信息点。

## 最小示例

```json
{
  "title": "示例",
  "size": [720, 1280],
  "fps": 30,
  "scenes": [
    {
      "id": 1,
      "narration": "先看第一个概念，Agent 到底是什么。",
      "visual": {
        "type": "title",
        "heading": "Agent 是什么",
        "lines": ["AI 小白系列", "从会聊天到会干活", "Agent / API / API Key"],
        "meta": {"accent": "blue"}
      }
    },
    {
      "id": 2,
      "narration": "网页版像打电话咨询，Agent 像真的雇了个人替你干活。",
      "visual": {
        "type": "compare",
        "heading": "两者的区别",
        "lines": ["网页版：打电话问顾问", "你问它答，活还得自己干", "Agent：雇了个员工", "你说目标，它自己动手"],
        "meta": {"accent": "purple"}
      }
    }
  ]
}
```

## 检查清单

写完 script.json 后自查：

- 分镜数与目标时长匹配（见上表）
- 每个 scene 都有 `narration` 与 `visual.type`
- `heading` ≤12 字，`lines` 每项 ≤18 字
- `table` / `terms` 的每项都带冒号
- `compare` 的 lines 是偶数条
- `steps` 有 `meta.step_no`
- 旁白里没有 emoji、括号、网址
