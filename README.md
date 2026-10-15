# MoviePolit — MoviePilot 流媒体上线监控插件

一个可直接发布的 **MoviePilot V2 插件**：定时监控指定地区流媒体平台（iQIYI / WeTV / Netflix /
Disney+ / Prime Video …）**即将上线**与**近期上线**的电影、剧集，
并通过 MoviePilot 已启用的通知渠道推送消息。

```
moviepolit-plugin/
├─ package.v2.json                  # 插件市场清单（自建插件仓库用）
├─ plugins.v2/
│  └─ moviepolit/
│     ├─ __init__.py                # 插件本体（唯一必需文件）
│     └─ README.md                  # 插件说明（市场中展示）
└─ README.md                        # 本文件：安装 / 配置 / 发布说明
```

## 快速开始

1. **启用通知渠道**：MoviePilot →「设置 → 通知」里配置并启用至少一个渠道（微信、企业微信、Telegram、WxPusher 等）。
2. **确认 TMDB 可用**：MoviePilot →「设置 → 媒体识别 → TheMovieDb」保证 API Key 正常（可在设置页点测试）。
3. **安装插件**：见下方「安装方式」。
4. **配置插件**：启用插件 → 填写检测时间与监控平台 → 勾选「立即运行一次」验证效果。

## 安装方式

### A. 作为自建插件仓库安装（推荐，可自动更新）

1. 新建一个 GitHub 仓库，把本目录内容**原样**推上去（保持 `package.v2.json` + `plugins.v2/moviepolit/` 结构）。
2. MoviePilot →「设置 → 插件 → 插件市场」→ 添加插件仓库地址
   `https://github.com/<你的用户名>/<仓库名>`。
3. 在市场里搜索「流媒体上线监控」安装；或在「本地插件」列表里直接启用。

### B. 手动放置

把 `plugins.v2/moviepolit` 目录复制为 MoviePilot 插件目录下的 **全小写** 目录名 `moviepolit`：

```
<MoviePilot 根目录>/app/plugins/moviepolit/__init__.py
```

Docker 部署一般对应容器内 `/app/app/plugins/moviepolit/__init__.py`（可用 `docker cp` 或映射卷放入）。
之后在「设置 → 插件」中重载插件或重启容器，再到本地插件里启用。

> 插件只依赖 MoviePilot 自带的 `pytz` / `apscheduler` / `requests`，**没有 requirements.txt，也不需要装任何依赖**。

## 工作原理

| 通道 | TMDB 查询 | 说明 |
| --- | --- | --- |
| 电影 · 即将上线 | `/discover/movie` 按 `release_date` 未来窗口 + `with_release_type=4` | 数字版（流媒体）发行日期 |
| 电影 · 近期上线 | 同上，`release_date` 回溯窗口 | 最近已上线数字版 |
| 剧集 · 即将首播 | `/discover/tv` 按 `first_air_date` 未来窗口 | 新剧 / 新季首播 |
| 剧集 · 近期上线 | 同上，回溯窗口 | 最近首播 |
| 剧集 · 近期更新 | `/discover/tv` 按 `air_date` 回溯窗口 | 最近有新剧集上线 |

所有查询都带 `watch_region=<地区>`（电影另带 `region=<地区>`），配置了平台时再带 `with_watch_providers=<平台ID>`：

- 平台名通过 `/watch/providers/movie|tv?watch_region=<地区>` 自动解析成 ID（中英文名都匹配，缓存 7 天），
  也支持直接填 TMDB 平台 ID。
- 电影会对候选影片逐条回查 `/movie/{id}/release_dates`，取该地区 type=4（数字版）的真实日期覆盖显示值，
  并剔除回查不到或不在窗口内的条目；结果在一次检测内按 `(地区, id)` 缓存。
- 每条作品的实际上线平台通过 `/{movie|tv}/{id}/watch/providers` 补充（可在配置里关闭以减少请求）。
- 通知通过 `self.post_message(mtype=NotificationType.Plugin, ...)` 发送，即走系统通知渠道配置。

## 配置项一览

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 启用插件 | 关 | 打开才会注册定时服务 |
| 立即运行一次 | 关 | 保存后立刻跑一次 |
| 消息类型 | 插件消息 | NotificationType |
| 检测时间 | `0 9 * * *` | 5 段 cron |
| 上线地区 | TW | CN/HK/TW/US/JP/KR/GB/SG；CN 无平台数据 |
| 监控平台 | 空 | 逗号分隔，留空 = 全部（推荐） |
| 监控电影 / 监控剧集 | 开 | 查询通道开关 |
| 含近期更新剧集 | 开 | 增加 `air_date` 通道 |
| 电影仅统计数字版上线 | 开 | 关闭 = 不限发行类型 |
| 即将上线窗口 / 近期上线窗口 | 30 / 7 天 | 时间窗口 |
| 单次推送最大条数 | 30 | 分组配额，影响请求量 |
| 每种查询抓取页数 | 1 | 每页 20 条 |
| 最低 TMDB 评分 | 0 | 0 = 不过滤 |
| 原始语言 | 空 | `zh` / `en` / `ja` … |
| 已推送过的不再重复推送 | 开 | 去重记录保留 180 天 |
| 显示每条作品的上线平台 | 开 | 每条多一次 TMDB 请求 |
| 详情页历史保留天数 | 30 | 推送历史条数 |

## 已知边界

1. **TMDB 没有中国大陆平台数据**：`watch_region=CN` 返回 0 条平台，且腾讯视频 / 优酷 / 芒果TV /
   哔哩哔哩 完全不在 TMDB 平台库中（全库仅 `iQIYI`、`WeTV` 等少数华语平台）。因此默认地区是 **TW**，
   默认「监控平台」留空；请使用 HK / TW / US / JP / KR / GB / SG 这类有平台数据的地区。
2. **平台数据在「即将上线」窗口内极少**（实测 US 未来 30 天：不限平台 183 条，Netflix 仅 2 条，
   Disney+ / Prime 为 0）。若按平台筛选在窗口内一条都没查到，插件会自动退回全部平台重查并在通知里说明。
3. 电影默认只看数字版发行日期（`with_release_type=4`）。该过滤的窗口**只认 `region` 不认 `watch_region`**，
   且即使传了 `region` 仍可能有少量条目带出院线日期（实测 TW 12 条中 3 条），因此插件会对候选影片
   逐条回查 `release_dates` 取 type=4 的真实日期，回查不到或不在窗口内的影片不会出现在列表。
4. “剧集 · 近期更新” 依赖 TMDB 的分集播出数据，仅对数据完整的剧集有效。
5. 插件不做下载、不管订阅，只做“发现 + 通知”；把通知里感兴趣的作品手动加订阅即可。

## 发布前的自定义

- `plugins.v2/moviepolit/__init__.py`：`plugin_author`、`plugin_icon`、`plugin_order`。
- `package.v2.json`：`author`、`icon`、`description`、`history`。
- 版本升级时同时改 `plugin_version` 与 `package.v2.json` 的 `version` + `history`，市场才会提示更新。
