# astrbot_plugin_calendar

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 编写的日历插件：查询法定节假日、调休补班、周末，以及元宵、七夕、重阳等**传统节日与纪念日**，并注册 LLM 工具供大模型在对话中自主调用。数据本地缓存、每月自动更新，断网也能用。

> 要求 AstrBot `>= 4.16`。

## ✨ 功能特性

- **法定节假日 / 调休**：数据来自 [holiday-cn](https://github.com/NateScarlet/holiday-cn)（跟随国务院公告），经 jsDelivr CDN 分发，带 ETag 条件请求，无变更时不重复下载
- **传统节日 / 纪念日**：数据来自 [ChinaCalendar](https://github.com/YangH9/ChinaCalendar) 的 `cal_festival.ics` 订阅，内置轻量 iCal 解析器（支持 RFC 5545 折叠行、多天事件逐日展开），与法定数据相互独立
- **三级回退**：本地缓存 → chinese-calendar 离线库 → 仅周末判断，任何一级可用都不会让查询崩溃
- **每月自动更新**：可在配置中指定检查日与时刻（默认每月 1 日凌晨 3 点），法定与节日数据一并刷新
- **LLM 自主调用**：注册 4 个函数工具，大模型可根据用户自然语言自动调用，无需记指令
- **全路径容错**：非法日期/月份输入返回友好提示；数据源失效时静默降级并记录日志

## 📋 指令

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `/节日` | 查询今天的状态 | `/节日` |
| `/节日查询 <YYYY-MM-DD>` | 查询指定日期 | `/节日查询 2026-10-01` |
| `/下个节日` | 下一个法定节假日及倒计时 | `/下个节日` |
| `/节日列表 [YYYY-MM]` | 某月法定节假日/调休概览（缺省当月） | `/节日列表 2026-10` |
| `/传统节日 [YYYY-MM]` | 某月传统节日与纪念日（缺省当月） | `/传统节日 2026-02` |

### 输出示例

```
> /节日

📅 2026-09-26（星期六）
├ 状态：🎉 法定节假日：中秋节
├ 是否休息：是 ✅
└ 是否调休：否
```

```
> /节日查询 2026-10-10

📅 2026-10-10（星期六）
├ 状态：💼 调休补班日（国庆节）
├ 是否休息：否 ❌
└ 是否调休：是 🔄
```

传统节日会合入日期状态输出（与法定节日同名时自动去重，避免出现两行"春节"）：

```
> /节日查询 2026-03-03

📅 2026-03-03（星期二）
├ 状态：💼 工作日
├ 是否休息：否 ❌
├ 传统节日：元宵节
└ 是否调休：否
```

```
> /传统节日 2026-02

🏮 2026年2月 传统节日与纪念日

• 02-10（星期二）：北方小年
• 02-11（星期三）：南方小年
• 02-16（星期一）：除夕
• 02-17（星期二）：春节
……
```

## 🤖 LLM 工具

接入大模型后（AstrBot 的 LLM 能力），以下工具可被模型自主调用：

| 工具名 | 参数 | 说明 |
| --- | --- | --- |
| `calendar_check_date` | `date`（YYYY-MM-DD，可空） | 查询某天节假日/调休状态，留空为今天，支持"今天" |
| `calendar_next_holiday` | 无 | 下一个法定节假日及倒计时 |
| `calendar_month_list` | `month`（YYYY-MM，可空） | 某月法定节假日/调休概览 |
| `calendar_traditional_festival` | `month`（YYYY-MM，可空） | 某月传统节日与纪念日（含数九、三伏时段） |

示例对话：用户问"元宵节是哪天？"，模型会自动调用 `calendar_traditional_festival` 并依据返回结果作答。

## ⚙️ 配置项

在 AstrBot 插件配置面板中修改（均有默认值，不改也能用）：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 是否启用插件 |
| `cache_dir` | string | `""` | 缓存目录，相对路径基于 AstrBot 的 `data` 目录；留空使用 `data/plugin_data/astrbot_plugin_calendar` |
| `cdn_base` | string | jsDelivr | holiday-cn 数据源地址，国内推荐默认值 `https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master` |
| `auto_update` | bool | `true` | 每月自动检查更新 |
| `update_day` | int | `1` | 每月几号检查（1-28） |
| `update_hour` | int | `3` | 检查时刻（0-23），建议凌晨低峰 |
| `fallback_enable` | bool | `true` | 缓存不可用时是否回退到 chinese-calendar 库 |
| `festival_enable` | bool | `true` | 启用传统节日/纪念日数据 |
| `festival_ics_url` | string | ChinaCalendar | 传统节日 iCal 订阅地址 |

## 📦 安装

在 AstrBot 中通过插件市场搜索 `astrbot_plugin_calendar`，或从仓库安装：

```
https://github.com/aName2262/astrbot_plugin_calendar
```

依赖（httpx、chinese-calendar）已声明在 `requirements.txt`，AstrBot 会自动安装。

## 🗃 数据与缓存

- 缓存位置：`data/plugin_data/astrbot_plugin_calendar/`（可用 `cache_dir` 自定义）
  - `<年份>.json` + `<年份>.json.etag`：法定节假日数据（holiday-cn）
  - `cal_festival.ics` + `cal_festival.ics.etag`：传统节日日历（ChinaCalendar）
- 首次启动会下载当年与明年的法定数据及节日日历；之后每月定时用 ETag 条件请求校验，无变更时只消耗一次 304
- 断网时查询走本地缓存；缓存也不可用则回退到 chinese-calendar（覆盖年份有限，超出后按周末判断）

## ⚠️ 数据覆盖范围说明

- **法定节假日**（holiday-cn）：跟随国务院公告发布节奏，通常每年 11 月前后公布次年安排；未公布年份的查询会回退
- **传统节日**（ChinaCalendar）：滚动收录约最近 3 年（当前 2025–2027），查询范围外的年份会返回"数据源暂未收录"提示，不影响法定数据

## 🙏 数据来源与致谢

本插件的数据并非自行整理，向以下开源项目致谢：

- [holiday-cn](https://github.com/NateScarlet/holiday-cn) —— 法定节假日与调休数据（经 jsDelivr CDN 获取）
- [ChinaCalendar](https://github.com/YangH9/ChinaCalendar) —— 传统节日与纪念日 iCal 订阅（`cal_festival.ics`）
- [chinese-calendar](https://github.com/LKI/chinese-calendar) —— 离线回退数据源

## 📄 目录结构

```
astrbot_plugin_calendar/
├── main.py            # 插件本体
├── metadata.yaml      # 插件元数据
├── _conf_schema.json  # 配置面板 schema
├── requirements.txt   # 依赖声明
└── README.md
```

## 开发

代码通过 `ruff check` 与 `ruff format` 检查；提交前请保持 lint 干净。
