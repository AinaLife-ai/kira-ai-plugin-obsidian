# kira-ai-plugin-obsidian

**Obsidian 笔记工具** — 赋予 KiraAI 读写 Obsidian 笔记库（vault）的能力，并内置**学习监督与日程扩展**（v1.1.0）。

> 本仓库为 skyzhishui/kira-ai-plugin-obsidian 的扩展分支：保留原 7 个笔记工具，新增学习计划/打卡/复盘 3 个工具 + 定时提醒。

## 功能

| 工具 | 描述 |
|------|------|
| `note_list` | 列出指定目录下的笔记与文件夹（分页） |
| `note_read` | 读取笔记：content / metadata / structure 三种模式 |
| `note_write` | 创建或全量替换笔记 |
| `note_append` | 追加内容到笔记末尾（日记、记录类） |
| `note_patch` | 局部修改（标题/块引用/frontmatter 定位） |
| `note_delete` | 删除笔记（不可逆） |
| `note_search` | 搜索笔记：simple / dataview DQL / jsonlogic |
| `study_schedule` | **学习日程**：把任务排进计划笔记，格式兼容 Obsidian Gantt Calendar / Tasks 插件（`- [ ] 描述 ⏫ 🛫 2026-09-28 09:00 📅 2026-09-28 11:00`），自动清理当天旧任务避免重复 |
| `study_checkin` | **学习打卡**：按关键词把未完成任务标记为 `[x]` 并追加 `✅ 完成日期` |
| `study_report` | **学习复盘**：统计完成率/今日截止/过期任务，过期未完成自动顺延到明天，输出进度建议 |

### 定时提醒（主动推送）

启用 `reminder_enabled` 并填写 `push_sid` 后，插件后台每分钟检查计划文件：

- 任务开始前 `remind_before_minutes` 分钟提醒「该开始了」
- 每天 `report_time`（默认 21:00）自动推送当日进度复盘

任务格式与 [sustcsugar/obsidian-gantt-calendar](https://github.com/sustcsugar/obsidian-gantt-calendar) 兼容，在 Obsidian 里装 Gantt Calendar 即可看到甘特图可视化。

## 配置

依赖 Obsidian 社区插件
[Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api)，需先在 Obsidian 中安装并启用。

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `base_url` | Local REST API 服务地址（同机 `http://127.0.0.1:27123`） | 空 |
| `api_key` | Local REST API 生成的密钥 | 空 |
| `timeout_seconds` | 单次请求超时秒数 | 15 |
| `max_results` | note_search 最多展示条数 | 8 |
| `summary_max_length` | 搜索摘要截断长度 | 150 |
| `max_output_chars` | 工具输出最大字符数 | 8000 |
| `plan_file` | 学习计划笔记文件名（vault 相对路径） | `学习计划.md` |
| `push_sid` | 主动提醒目标会话（如 `qq:dm:123456` / `qq:gm:123456789`），留空不推送 | 空 |
| `reminder_enabled` | 开启定时提醒 | false |
| `remind_before_minutes` | 任务开始前提前提醒分钟数 | 10 |
| `report_time` | 每日复盘推送时间（HH:MM，24h） | 21:00 |

### 获取 api_key

1. Obsidian → 设置 → 第三方插件，安装并启用 **Local REST API**
2. 插件设置页将认证模式设为 required，复制 API Key
3. 保持 Obsidian 运行，把服务地址与 API Key 填入 KiraAI WebUI 插件配置

## 安装

1. KiraAI WebUI → 插件页 → 从 GitHub 安装，填入 `https://github.com/AinaLife-ai/kira-ai-plugin-obsidian`
2. 也可以下载本仓库 zip，通过 WebUI 上传安装
3. 安装完成后在插件配置页填入 `base_url`、`api_key`，如需定时提醒再填 `push_sid` 并打开 `reminder_enabled`

## 使用示例

```
「帮我安排今天的学习：复习高数 9:00-11:00 高优先级；背单词 14:00-15:00」
  → bot 调 study_schedule 写入 学习计划.md

「高数复习完了，打卡」
  → bot 调 study_checkin 标记完成

「今天学习情况怎么样」
  → bot 调 study_report 输出完成率，过期任务自动顺延到明天
```

## 依赖

无额外依赖。HTTP 客户端复用 KiraAI 本体自带的 `httpx`。

## 信息

- **插件 ID**: `kira-ai-plugin-obsidian`
- **版本**: 1.1.0
- **原作者**: skyzhishui（笔记工具部分）
- **扩展**: ainali / 爱奈丽（学习监督扩展）