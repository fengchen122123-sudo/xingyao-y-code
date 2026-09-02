---
name: tokenrhythm-image
description: Use when 用户要求生成图片、画图、海报、封面、配图，或要求修改/编辑已有图片（垫图改图）且需要 TokenRhythm（基元律动）模型时
triggers: 生图,画图,海报,封面,配图,改图,垫图
---

# TokenRhythm（基元律动）生图技能

## 前置条件

- 插件 `tokenrhythm_image` 已在插件页启用（默认关闭，需用户手动开启）。
- `~/.ycode/config.json` 的 providers 中已有 base_url 含 `tokenrhythm.studio` 的提供商条目并填入 API Key（密钥为 DPAPI 加密存储，任何环节都不要读取、复述或写出密钥内容）。
- 未启用或未配置时：告知用户去插件页开启插件、到设置页添加该提供商并填 Key，不要尝试绕过，也不要反复重试。

## 工具用法

- 文生图：`tokenrhythm_generate_image`，参数 prompt（必填）、size（1024x1024 / 1280x720 / 720x1280 / 1280x1280 / 1024x1792 / 1792x1024）、model（qwen-image-2.0 或 wan2.7-image）、n（1-4，每张 ¥0.20）、save_dir。耗时约 5~17 秒。
- 改图：`tokenrhythm_edit_image`，参数 prompt 与 image 必填（image 为原图完整本地路径，1 张、≤10MB、jpg/jpeg/png），可选 size / model / save_dir。
- @ 垫图：用户消息里 @图片文件 时，附件提示会给出完整路径，把该路径直接传给 `tokenrhythm_edit_image` 的 image 参数。

## 展示要求

工具成功返回后，必须在回复中直接用 Markdown 图片语法展示，不要只给路径：
`![生成的图片](本地路径)`

## 安全红线

不把任何密钥内容写入对话、代码或文件；插件只从用户配置运行时读取密钥。
