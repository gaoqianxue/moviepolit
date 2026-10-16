"""
MoviePolit - 流媒体上线监控

监控指定地区流媒体平台（Netflix / Disney+ / Prime Video / iQIYI / WeTV ...）
即将上线与近期上线的电影、剧集，通过 MoviePilot 已启用的通知渠道推送。

数据来源：
  * 海外/港台流媒体：TheMovieDb（复用 MoviePilot「设置 → 媒体识别 → TheMovieDb」
    中的 API Key、API 域名、图片域名与代理配置，无需额外填写任何密钥）。
  * 国内平台（B站）：哔哩哔哩番剧 / 国创放送表。国内平台中只有 B站 提供公开的
    排播接口且无需鉴权；接口硬限制为 before=0、after≤7，即只能看未来 8 天。
  * 国内平台（豆瓣）：豆瓣移动端 rexxar 接口的「近期上线」集合（国产剧 / 热门剧集 /
    热门综艺）。用于补齐腾讯视频 / 爱奇艺 / 优酷 / 芒果TV 的上新信息 —— 这些平台
    既无公开 API，也不在 TMDB 收录范围内。豆瓣给出的是**已经上线/正在可看**的
    时间（pubdate），不提供未来的流媒体排播日期，因此分组一律标注「新上线」。

已知边界（由数据源决定，非插件缺陷）：
  * TMDB 的流媒体平台数据只覆盖部分国家/地区；中国大陆（CN）没有任何平台数据，
    腾讯视频、优酷、芒果TV、哔哩哔哩未被 TMDB 收录，因此无法作为筛选条件。
    默认区域取台湾（TW），请按需改为 HK / US / JP / KR / GB / SG。
  * 电影的流媒体上线日需回查 /movie/{id}/release_dates 的数字版（type=4）记录，
    不能直接使用 discover 返回的 release_date（那是首要/院线日期）。
  * 豆瓣集合是院线/首播语义：带平台信息的条目均为「已上线」，pre_playable_date
    恒为空。故豆瓣条目只按 pubdate 归入「最近 N 天」窗口，不承诺未来档期。
  * 豆瓣电影集合（movie_latest）实测平台覆盖率为 0，故不接入；电影交由 TMDB 覆盖。
"""

import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from app.utils.http import RequestUtils


class MoviePolit(_PluginBase):
    """
    MoviePilot V2 插件：流媒体平台上线监控 + 通知推送
    """

    # ===== 插件元信息 =====
    plugin_name = "流媒体上线监控"
    plugin_desc = ("监控指定地区流媒体平台即将上线 / 近期上线的电影、剧集，"
                   "并监控国内 B站 番剧 / 国创排播与豆瓣新上线（腾讯视频 / 爱奇艺 / "
                   "优酷 / 芒果TV），通过 MoviePilot 通知渠道推送。")
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.jpg"
    plugin_version = "1.2.0"
    plugin_author = "MoviePolit"
    plugin_config_prefix = "moviepolit_"
    plugin_order = 21
    auth_level = 1

    # ===== 默认配置（类属性兜底，保证 get_service 在 init_plugin 之前也有值）=====
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "0 9 * * *"
    _region: str = "TW"
    _providers: str = ""
    _include_movie: bool = True
    _include_tv: bool = True
    _include_tv_airing: bool = True
    _strict_digital: bool = True
    # 国内平台（B站）监控：番剧 types=1，国创 types=4
    _include_bili: bool = True
    _include_bili_anime: bool = True
    _include_bili_guochuang: bool = True
    # 国内平台（豆瓣）新上线：剧集为主，语义为「近期可看」而非「即将上线」
    _include_douban: bool = True
    _include_douban_show: bool = True
    _days_ahead: int = 30
    _days_back: int = 7
    _max_items: int = 30
    _pages: int = 1
    _min_vote: float = 0.0
    _original_language: str = ""
    _dedupe: bool = True
    _show_provider_detail: bool = True
    _history_days: int = 30
    _msgtype: str = ""

    # 数字版日期回查缓存 {(地区, tmdb_id): 日期或 None}，仅在一次 check() 内有效
    _digital_cache: Optional[Dict[Tuple[str, Any], Optional[str]]] = None

    # ===== 插件数据 key =====
    _KEY_LATEST = "latest"
    _KEY_PUSHED = "pushed"
    _KEY_HISTORY = "history"
    _KEY_PROVIDERS = "providers"

    # 去重记录保留天数
    _DEDUPE_KEEP_DAYS = 180
    # 平台名称缓存有效期（秒）
    _PROVIDER_CACHE_TTL = 7 * 24 * 3600

    # ===== 国内平台（B站）放送表 =====
    # B站放送表接口硬限制：before 只能为 0、after 上限 7，即最多看未来 8 天。
    # 无需任何鉴权，types=1 番剧、types=4 国创（types=2/3 无数据）。
    _BILI_TIMELINE = "https://api.bilibili.com/pgc/web/timeline"
    _BILI_SEASON = "https://api.bilibili.com/pgc/view/web/season"
    _BILI_AFTER_MAX = 7
    # B站条目追番数阈值（低于该值的新番信息量过低，默认不过滤）
    _BILI_MIN_FAVES = 0

    # ===== 国内平台（豆瓣）新上线 =====
    # 豆瓣 rexxar 移动端接口无需鉴权；旧官方 API（api.douban.com/v2）已 403 要求授权。
    # 语义限制：豆瓣提供的是「当前/近期可看」而非「即将上线」——可用集合的元数据
    # 明确写为「院线即将上映」，条目均为已上线（is_released=True），pubdate 是首播日。
    # 因此豆瓣通道只做「近期新上线」，不做「即将上线」。
    # 实测平台字段覆盖率：tv_domestic 19/20、tv_hot 13/20、show_hot 8/20；
    # movie_latest 与 tv_korean 均为 0/20（无平台数据），故不纳入。
    _DOUBAN_API = "https://m.douban.com/rexxar/api/v2"
    _DOUBAN_COLLECTIONS = (
        ("tv_domestic", "国产剧"),
        ("tv_hot", "热门剧集"),
        ("show_hot", "热门综艺"),
    )
    # 从 vendors 的 app_bundle_id 推断平台名（linewatches 缺失时的兜底）
    _DOUBAN_BUNDLE_MAP = {
        "com.tencent.qqlive": "腾讯视频",
        "com.iqiyi.video": "爱奇艺",
        "com.qiyi.video": "爱奇艺",
        "com.youku.phone": "优酷",
        "com.hunantv.imgo.activity": "芒果TV",
        "tv.danmaku.bili": "哔哩哔哩",
    }

    # 区域下拉
    _REGIONS = [
        {"title": "中国大陆 (CN)", "value": "CN"},
        {"title": "中国香港 (HK)", "value": "HK"},
        {"title": "中国台湾 (TW)", "value": "TW"},
        {"title": "美国 (US)", "value": "US"},
        {"title": "日本 (JP)", "value": "JP"},
        {"title": "韩国 (KR)", "value": "KR"},
        {"title": "英国 (GB)", "value": "GB"},
        {"title": "新加坡 (SG)", "value": "SG"},
    ]

    # =====================================================================
    #  生命周期
    # =====================================================================
    def init_plugin(self, config: dict = None):
        """插件启用 / 保存配置时调用"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = (config.get("cron") or "0 9 * * *").strip()
        self._region = (config.get("region") or "TW").strip().upper()
        self._providers = (config.get("providers") or "").strip()
        self._include_movie = bool(config.get("include_movie", True))
        self._include_tv = bool(config.get("include_tv", True))
        self._include_tv_airing = bool(config.get("include_tv_airing", True))
        self._strict_digital = bool(config.get("strict_digital", True))
        self._include_bili = bool(config.get("include_bili", True))
        self._include_bili_anime = bool(config.get("include_bili_anime", True))
        self._include_bili_guochuang = bool(config.get("include_bili_guochuang", True))
        self._include_douban = bool(config.get("include_douban", True))
        self._include_douban_show = bool(config.get("include_douban_show", True))
        self._days_ahead = self._to_int(config.get("days_ahead"), 30, 0, 365)
        self._days_back = self._to_int(config.get("days_back"), 7, 0, 365)
        self._max_items = self._to_int(config.get("max_items"), 30, 1, 100)
        self._pages = self._to_int(config.get("pages"), 1, 1, 10)
        self._min_vote = self._to_float(config.get("min_vote"), 0.0)
        self._original_language = (config.get("original_language") or "").strip().lower()
        self._dedupe = bool(config.get("dedupe", True))
        self._show_provider_detail = bool(config.get("show_provider_detail", True))
        self._history_days = self._to_int(config.get("history_days"), 30, 1, 365)
        self._msgtype = config.get("msgtype") or ""

        # 立即运行一次
        if self._onlyonce:
            self._onlyonce = False
            self._save_config()
            logger.info("流媒体上线监控：立即运行一次")
            try:
                self.check()
            except Exception as e:
                logger.error(f"流媒体上线监控：立即运行失败：{e}")

    def _save_config(self):
        """保存当前配置快照"""
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "region": self._region,
            "providers": self._providers,
            "include_movie": self._include_movie,
            "include_tv": self._include_tv,
            "include_tv_airing": self._include_tv_airing,
            "strict_digital": self._strict_digital,
            "include_bili": self._include_bili,
            "include_bili_anime": self._include_bili_anime,
            "include_bili_guochuang": self._include_bili_guochuang,
            "include_douban": self._include_douban,
            "include_douban_show": self._include_douban_show,
            "days_ahead": self._days_ahead,
            "days_back": self._days_back,
            "max_items": self._max_items,
            "pages": self._pages,
            "min_vote": self._min_vote,
            "original_language": self._original_language,
            "dedupe": self._dedupe,
            "show_provider_detail": self._show_provider_detail,
            "history_days": self._history_days,
            "msgtype": self._msgtype,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        """本插件没有需要停止的常驻服务"""
        pass

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, number))

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # =====================================================================
    #  定时服务
    # =====================================================================
    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时检测服务"""
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as e:
            logger.error(f"流媒体上线监控：cron 表达式非法 [{self._cron}]：{e}，回退默认 0 9 * * *")
            trigger = CronTrigger.from_crontab("0 9 * * *")
        return [{
            "id": "MoviePolitCheck",
            "name": "流媒体上线检测",
            "trigger": trigger,
            "func": self.check,
            "kwargs": {},
        }]

    # =====================================================================
    #  配置表单
    # =====================================================================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置页面（Vuetify JSON）"""
        msg_options = [{"title": item.value, "value": item.name} for item in NotificationType]
        return [
            {
                "component": "VForm",
                "content": [
                    # 第一行：启用 / 立即运行
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "onlyonce", "label": "立即运行一次"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSelect",
                                     "props": {"model": "msgtype", "label": "消息类型",
                                               "items": msg_options, "clearable": True}}
                                ],
                            },
                        ],
                    },
                    # 第二行：cron / 区域
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "cron", "label": "检测时间（5 段 cron）",
                                               "placeholder": "0 9 * * *"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSelect",
                                     "props": {"model": "region", "label": "上线地区",
                                               "items": self._REGIONS}}
                                ],
                            },
                        ],
                    },
                    # 第三行：监控平台
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "providers",
                                               "label": "监控平台（逗号分隔，留空 = 该地区全部平台）",
                                               "placeholder": "iQIYI,WeTV,Netflix,Disney+"}}
                                ],
                            }
                        ],
                    },
                    # 第四行：检测范围
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_movie", "label": "监控电影"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_tv", "label": "监控剧集"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_tv_airing",
                                               "label": "含近期更新剧集"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "strict_digital",
                                               "label": "电影仅统计数字版上线"}}
                                ],
                            },
                        ],
                    },
                    # 第五行：国内平台（B站）
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_bili",
                                               "label": "监控国内平台（B站）"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_bili_anime",
                                               "label": "B站番剧"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_bili_guochuang",
                                               "label": "B站国创（国产动画）"}}
                                ],
                            },
                        ],
                    },
                    # 第五行之二：国内平台（豆瓣）
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_douban",
                                               "label": "国内平台新上线（豆瓣）"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "include_douban_show",
                                               "label": "豆瓣含综艺"}}
                                ],
                            },
                        ],
                    },
                    # 第六行：时间窗口与条数
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "days_ahead", "type": "number",
                                               "label": "即将上线窗口（天）", "placeholder": "30"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "days_back", "type": "number",
                                               "label": "近期上线窗口（天）", "placeholder": "7"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "max_items", "type": "number",
                                               "label": "单次推送最大条数", "placeholder": "30"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "pages", "type": "number",
                                               "label": "每种查询抓取页数", "placeholder": "1"}}
                                ],
                            },
                        ],
                    },
                    # 第七行：过滤条件
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "min_vote", "type": "number",
                                               "label": "最低 TMDB 评分（0 = 不过滤）",
                                               "placeholder": "0"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "original_language",
                                               "label": "原始语言（en/zh/ja...，留空 = 不限）",
                                               "placeholder": ""}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "history_days", "type": "number",
                                               "label": "详情页历史保留天数", "placeholder": "30"}}
                                ],
                            },
                        ],
                    },
                    # 第八行：通知细节
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "dedupe", "label": "已推送过的不再重复推送"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "show_provider_detail",
                                               "label": "显示每条作品的上线平台"}}
                                ],
                            },
                        ],
                    },
                    # 说明
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VAlert",
                                     "props": {
                                         "type": "info",
                                         "variant": "tonal",
                                         "text": "数据来自 TheMovieDb，复用「设置 → 媒体识别 → TheMovieDb」中的 "
                                                 "API Key，无需额外配置。推送走 MoviePilot 通知渠道，请先在"
                                                 "「设置 → 通知」启用渠道。"
                                                 "国内平台方面，哔哩哔哩的番剧 / 国创排播走 B站公开放送表接口"
                                                 "（无需鉴权、无需 Key），最多覆盖未来 8 天；腾讯视频、优酷、"
                                                 "芒果TV 没有公开接口，也无法通过 TMDB 覆盖，暂不支持。"
                                                 "注意：TMDB 的流媒体平台数据只覆盖部分国家/地区，中国大陆（CN）"
                                                 "目前没有任何平台数据，腾讯视频、优酷、芒果TV、哔哩哔哩也未被 "
                                                 "TMDB 收录，无法作为筛选条件；建议选择香港（HK）、台湾（TW）、"
                                                 "美国（US）等地区，并用 iQIYI、WeTV、Netflix 等 TMDB 已收录的"
                                                 "名称。平台名称支持中文名、英文名或 TMDB 平台 ID；识别失败的"
                                                 "平台会在通知里提示。即将上线的作品 TMDB 往往还没关联平台数据，"
                                                 "若按平台筛选在窗口内一条都没查到，插件会自动退回「该地区全部"
                                                 "平台」重查并在通知里说明；想看得多建议把「监控平台」留空。"
                                                 "电影显示的是该地区的数字版（流媒体）上线日，插件会逐条回查 "
                                                 "TMDB 的 release_dates 校正日期，与院线上映日不同属正常现象。",
                                     }}
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "0 9 * * *",
            "region": "TW",
            "providers": "",
            "include_movie": True,
            "include_tv": True,
            "include_tv_airing": True,
            "strict_digital": True,
            "include_bili": True,
            "include_bili_anime": True,
            "include_bili_guochuang": True,
            "include_douban": True,
            "include_douban_show": True,
            "days_ahead": 30,
            "days_back": 7,
            "max_items": 30,
            "pages": 1,
            "min_vote": 0,
            "original_language": "",
            "dedupe": True,
            "show_provider_detail": True,
            "history_days": 30,
            "msgtype": "",
        }

    # =====================================================================
    #  主流程
    # =====================================================================
    def check(self):
        """检测流媒体上线情况并推送通知"""
        logger.info(f"流媒体上线监控：开始检测（区域={self._region} "
                    f"平台={self._providers or '全部'}）")

        provider_ids, unknown = self._resolve_provider_ids()
        if self._providers and not provider_ids:
            logger.warning("流媒体上线监控：配置的平台均无法识别，本次按『该地区全部平台』查询")

        # 数字版日期回查缓存，一次 check() 内复用，避免重复请求同一部电影
        self._digital_cache = {}

        # 待执行的查询通道
        passes: List[Tuple[str, str]] = []
        if self._include_movie:
            passes.append(("movie", "upcoming"))
            passes.append(("movie", "recent"))
        if self._include_tv:
            passes.append(("tv", "upcoming"))
            passes.append(("tv", "recent"))
            if self._include_tv_airing:
                passes.append(("tv", "airing"))
        # 国内平台（B站）是否参与本次检测
        bili_enabled = self._include_bili and (self._include_bili_anime
                                               or self._include_bili_guochuang)
        # 国内平台（豆瓣）新上线：与 B站相互独立，可单独开关
        douban_enabled = self._include_douban
        if not passes and not bili_enabled and not douban_enabled:
            logger.warning("流媒体上线监控：未勾选电影、剧集与国内平台，跳过本次检测")
            return

        # 查询
        grouped: Dict[Tuple[str, str], List[dict]] = {}
        seen = set()
        failures: List[str] = []
        # 记录成功返回的通道数：用于区分「全部通道失败」与「通道正常但无新内容」，
        # 避免 TMDB 正常、仅国内平台接口失败时误发「检测失败」告警。
        tmdb_ok = 0
        for mtype, phase in passes:
            try:
                found = self._discover(mtype, phase, provider_ids)
            except Exception as e:
                logger.error(f"流媒体上线监控：查询失败（{mtype}/{phase}）：{e}")
                failures.append(f"{self._phase_label(mtype, phase)}："
                                f"{self._short(str(e), 60) or '未知错误'}")
                continue
            tmdb_ok += 1
            bucket = grouped.setdefault((mtype, phase), [])
            for item in found:
                if item["key"] in seen:
                    continue
                seen.add(item["key"])
                bucket.append(item)

        # 平台筛选兜底：TMDB 的 watch_providers 在「即将上线」窗口内命中极少
        # （该窗口内多数条目还没有平台记录）。若按平台筛选一条都没查到，则退回
        # 「该地区全部平台」重查一次，避免用户填了平台后长期收不到推送。
        notes: List[str] = []
        if provider_ids and not any(grouped.values()):
            logger.warning(f"流媒体上线监控：按平台 {self._providers} 在窗口内未查到内容，"
                           f"本次退回『该地区全部平台』查询")
            for mtype, phase in passes:
                try:
                    found = self._discover(mtype, phase, [])
                except Exception as e:
                    logger.error(f"流媒体上线监控：查询失败（{mtype}/{phase}）：{e}")
                    failures.append(f"{self._phase_label(mtype, phase)}："
                                    f"{self._short(str(e), 60) or '未知错误'}")
                    continue
                bucket = grouped.setdefault((mtype, phase), [])
                for item in found:
                    if item["key"] in seen:
                        continue
                    seen.add(item["key"])
                    bucket.append(item)
            notes.append(f"平台「{self._providers}」在窗口内无内容，本次已按该地区全部平台查询")

        # 去重已推送 + 配额裁剪
        limit = self._max_items if self._max_items > 0 else 100
        # 配额按「TMDB 通道数 + 国内平台通道数」均分，避免某一侧独占推送额度
        channel_count = len(passes) + (1 if bili_enabled else 0) + (1 if douban_enabled else 0)
        quota = max(1, int(limit / channel_count) + (1 if limit % channel_count else 0))
        pushed = self.get_data(self._KEY_PUSHED)
        if not isinstance(pushed, dict):
            pushed = {}
        sections: List[Tuple[str, List[dict]]] = []
        for mtype, phase in passes:
            candidates = grouped.get((mtype, phase)) or []
            if self._dedupe:
                candidates = [item for item in candidates if item["key"] not in pushed]
            candidates = candidates[:quota]
            # 电影：用真实的数字版发行日覆盖院线日期，并剔除窗口外的条目
            if mtype == "movie" and self._strict_digital:
                candidates = self._confirm_digital(candidates, quota)
            if candidates:
                sections.append((self._phase_label(mtype, phase), candidates))

        # 国内平台（B站）：独立数据源，失败不影响 TMDB 通道
        bili_failures: List[str] = []
        bili_ok = False
        if bili_enabled:
            bili_items = self._bili_items(bili_failures)
            # 只要没记失败，就认为本次国内平台通道正常（可能确实无排播内容）
            bili_ok = not bili_failures
            if self._dedupe:
                bili_items = [item for item in bili_items if item["key"] not in pushed]
            bili_items = bili_items[:quota]
            if bili_items:
                sections.append((self._bili_section_label(), bili_items))
            for failure in bili_failures:
                if failure not in failures:
                    failures.append(failure)

        # 国内平台（豆瓣）新上线：与 B站相互独立，各自记录成败
        douban_failures: List[str] = []
        douban_ok = False
        if douban_enabled:
            douban_items = self._douban_items(douban_failures)
            douban_ok = not douban_failures
            if self._dedupe:
                douban_items = [item for item in douban_items if item["key"] not in pushed]
            douban_items = douban_items[:quota]
            if douban_items:
                sections.append((self._douban_section_label(), douban_items))
            for failure in douban_failures:
                if failure not in failures:
                    failures.append(failure)

        # 回查后条数可能减少，重新统计
        total = sum(len(items) for _, items in sections)

        # 补齐每条作品的上线平台
        if self._show_provider_detail:
            for _, items in sections:
                self._attach_providers(items)

        # 记录本次检测结果（无论是否有新内容，详情页都要能看到）
        all_items = [item for _, items in sections for item in items]
        self.save_data(self._KEY_LATEST, {
            "time": self._now().strftime("%Y-%m-%d %H:%M"),
            "count": total,
            "region": self._region,
            "providers": self._providers,
            "items": all_items[:60],
        })

        if not total:
            # 只有当所有通道都失败（如未配置 TMDB Key、网络不通）才发告警。
            # 若 TMDB 正常返回、只是没有新内容（或已被去重），则记日志即可，
            # 避免「国内平台接口偶发失败」被误报成整体检测失败。
            any_channel_ok = (tmdb_ok > 0) or bili_ok or douban_ok
            if failures and not any_channel_ok:
                logger.error(f"流媒体上线监控：检测失败：{failures}")
                try:
                    self.post_message(mtype=self._notification_type(),
                                      title="🎬 流媒体上线监控：检测失败",
                                      text="本次检测未能获取到数据：\n"
                                           + "\n".join(f"· {item}" for item in failures))
                except Exception as e:
                    logger.error(f"流媒体上线监控：发送失败通知出错：{e}")
            elif failures:
                # 部分通道失败但仍有通道正常：仅日志提示，不打扰用户
                logger.warning(f"流媒体上线监控：本次无新内容，部分通道失败：{failures}")
            else:
                logger.info("流媒体上线监控：本次没有新的可推送内容")
            return

        # 推送
        title = f"🎬 流媒体上线速递 · {self._region}"
        text = self._build_message(sections, unknown, failures, notes)
        image = next((item["poster"] for item in all_items if item.get("poster")), "")
        mtype = self._notification_type()
        try:
            self.post_message(mtype=mtype, title=title, text=text, image=image or None)
        except Exception as e:
            logger.error(f"流媒体上线监控：发送通知失败：{e}")
            return

        # 推送成功后再记录去重与历史
        if self._dedupe:
            today = self._today()
            cutoff = (self._now() - timedelta(days=self._DEDUPE_KEEP_DAYS)).strftime("%Y-%m-%d")
            merged = {k: v for k, v in pushed.items() if v and v >= cutoff}
            for item in all_items:
                merged[item["key"]] = today
            self.save_data(self._KEY_PUSHED, merged)

        self._append_history(total, all_items)
        logger.info(f"流媒体上线监控：推送完成，共 {total} 条")

    def _notification_type(self) -> NotificationType:
        if self._msgtype:
            try:
                return NotificationType[self._msgtype]
            except KeyError:
                pass
        return NotificationType.Plugin

    def _append_history(self, count: int, items: List[dict]):
        """记录推送历史（详情页展示）"""
        history = self.get_data(self._KEY_HISTORY)
        if not isinstance(history, list):
            history = []
        history.insert(0, {
            "time": self._now().strftime("%Y-%m-%d %H:%M"),
            "count": count,
            "titles": "、".join(f"《{item['title']}》" for item in items[:6]),
        })
        self.save_data(self._KEY_HISTORY, history[:max(1, self._history_days)])

    # =====================================================================
    #  消息排版
    # =====================================================================
    def _phase_label(self, mtype: str, phase: str) -> str:
        if mtype == "movie":
            if phase == "upcoming":
                return f"电影 · 即将上线（未来 {self._days_ahead} 天）"
            return f"电影 · 近期上线（最近 {self._days_back} 天）"
        if phase == "upcoming":
            return f"剧集 · 即将首播（未来 {self._days_ahead} 天）"
        if phase == "airing":
            return f"剧集 · 近期更新（最近 {self._days_back} 天）"
        return f"剧集 · 近期上线（最近 {self._days_back} 天）"

    def _bili_section_label(self) -> str:
        """国内平台分组标题。放送表最多覆盖未来 8 天（接口硬限制 after≤7）。"""
        kinds = []
        if self._include_bili_anime:
            kinds.append("番剧")
        if self._include_bili_guochuang:
            kinds.append("国创")
        return f"国内平台 · B站{'/'.join(kinds)}（未来 {self._BILI_AFTER_MAX + 1} 天）"

    def _douban_section_label(self) -> str:
        """豆瓣分组标题。语义为「近期新上线」而非「即将上线」——豆瓣不提供
        未来的流媒体排播日期，只有已上线条目的首播日。"""
        return f"国内平台 · 豆瓣新上线（最近 {self._days_back} 天）"

    def _build_message(self, sections: List[Tuple[str, List[dict]]],
                       unknown: List[str], failures: List[str],
                       notes: Optional[List[str]] = None) -> str:
        lines = [
            f"🎬 流媒体上线速递 · {self._region}",
            self._now().strftime("%Y-%m-%d %H:%M"),
            f"平台：{self._providers or '该地区全部平台'}",
            "",
        ]
        for section_title, items in sections:
            lines.append(f"【{section_title}】")
            lines.extend(self._format_line(item) for item in items)
            lines.append("")
        total = sum(len(items) for _, items in sections)
        # 数据源署名：只在确实出现了对应来源的条目时才列出，避免误导
        sources: List[str] = []
        all_items_flat = [item for _, items in sections for item in items]
        if any(item.get("source") not in ("bilibili", "douban")
               for item in all_items_flat):
            sources.append("TMDB")
        if any(item.get("source") == "bilibili" for item in all_items_flat):
            sources.append("哔哩哔哩")
        if any(item.get("source") == "douban" for item in all_items_flat):
            sources.append("豆瓣")
        lines.append(f"共 {total} 条 · 数据来源 {' / '.join(sources) or 'TMDB'}"
                     f" · 点击消息查看完整详情")
        if unknown:
            available = self._available_provider_names()
            lines.append("⚠️ 未识别的平台：" + "、".join(unknown)
                         + (f"（该地区可用：{'、'.join(available[:12])}）" if available else ""))
        for note in notes or []:
            lines.append(f"ℹ️ {note}")
        if failures:
            lines.append("⚠️ 部分查询失败：" + "；".join(failures))
        return "\n".join(lines)

    def _format_line(self, item: dict) -> str:
        parts = [f"· 《{item['title']}》"]
        if item.get("year"):
            parts.append(item["year"])
        if item.get("vote"):
            parts.append(f"{item['vote']}分")
        # 国内平台（B站）：显示集数与具体上线时刻，而非「首播/数字版」
        if item.get("source") == "bilibili":
            if item.get("episode"):
                parts.append(item["episode"])
            if item.get("pub_time"):
                parts.append(f"更新 {item['pub_time']}")
            # 追番数：接口对未统计的条目返回占位符「-」，需过滤掉
            follows = (item.get("follows") or "").strip()
            if follows and follows != "-":
                parts.append(follows)
        # 国内平台（豆瓣）：显示集数与上线日期；平台名已在 providers 里
        elif item.get("source") == "douban":
            if item.get("episode"):
                parts.append(item["episode"])
            if item.get("date"):
                parts.append(f"上线 {item['date'][5:]}")
        elif item.get("phase") == "airing":
            parts.append("近期有新集")
        elif item.get("date"):
            label = "数字版" if item.get("type") == "movie" else "首播"
            parts.append(f"{label} {item['date'][5:]}")
        if item.get("providers"):
            parts.append(item["providers"])
        return "  ".join(parts)

    # =====================================================================
    #  TMDB 查询
    # =====================================================================
    def _discover(self, mtype: str, phase: str, provider_ids: List[int]) -> List[dict]:
        """按通道查询 TMDB discover 接口"""
        today = self._now().date()
        params: Dict[str, Any] = {
            "watch_region": self._region,
            "include_adult": "false",
        }
        if provider_ids:
            params["with_watch_providers"] = "|".join(str(pid) for pid in provider_ids)
        if self._original_language:
            params["with_original_language"] = self._original_language

        if mtype == "movie":
            path = "/discover/movie"
            if self._strict_digital:
                # 4 = 数字版（流媒体上线）
                params["with_release_type"] = "4"
            # 注意：with_release_type 的窗口过滤只认 region，不认 watch_region。
            # 只传 watch_region 时窗口按“任意地区”的数字版日期匹配，会把院线日期
            # 混进结果；传 region 才会按该地区的发行日期过滤。
            params["region"] = self._region
            if phase == "upcoming":
                # 窗口从明天起算：与「近期上线」以今天为界互不重叠。若两边都含
                # 今天，page 1 会是同一批今天的条目，recent 分组在跨组去重中被
                # 整批清空（实网复现：tv/recent 命中 20 条但分组消失）。
                params["release_date.gte"] = (today + timedelta(days=1)).strftime("%Y-%m-%d")
                params["release_date.lte"] = (today + timedelta(days=self._days_ahead)).strftime("%Y-%m-%d")
                params["sort_by"] = "release_date.asc"
            else:
                params["release_date.gte"] = (today - timedelta(days=self._days_back)).strftime("%Y-%m-%d")
                params["release_date.lte"] = today.strftime("%Y-%m-%d")
                params["sort_by"] = "release_date.desc"
        else:
            path = "/discover/tv"
            if phase == "airing":
                params["air_date.gte"] = (today - timedelta(days=self._days_back)).strftime("%Y-%m-%d")
                params["air_date.lte"] = today.strftime("%Y-%m-%d")
                params["sort_by"] = "popularity.desc"
            elif phase == "upcoming":
                # 同电影：从明天起算，避免与「近期上线/近期更新」在今天重叠，
                # 否则今天首播的条目会把 recent 分组的候选整批去重掉。
                params["first_air_date.gte"] = (today + timedelta(days=1)).strftime("%Y-%m-%d")
                params["first_air_date.lte"] = (today + timedelta(days=self._days_ahead)).strftime("%Y-%m-%d")
                params["sort_by"] = "first_air_date.asc"
            else:
                params["first_air_date.gte"] = (today - timedelta(days=self._days_back)).strftime("%Y-%m-%d")
                params["first_air_date.lte"] = today.strftime("%Y-%m-%d")
                params["sort_by"] = "first_air_date.desc"

        items: List[dict] = []
        for page in range(1, self._pages + 1):
            params["page"] = page
            data = self._tmdb_get(path, params)
            if not data:
                if page == 1:
                    # 首页就失败：抛出由上层记录，避免静默当作“没有新内容”
                    raise RuntimeError(f"TMDB 查询失败：{path}")
                break
            results = data.get("results") or []
            for raw in results:
                item = self._build_item(raw, mtype, phase)
                if item:
                    items.append(item)
            if len(results) < 20:
                break

        logger.debug(f"流媒体上线监控：{mtype}/{phase} 命中 {len(items)} 条")
        return items

    def _confirm_digital(self, items: List[dict], limit: int) -> List[dict]:
        """
        电影：discover 返回的 release_date 是「首要 / 院线」日期，不是流媒体上线日。
        逐条回查 release_dates，取出本地区的数字版（type=4）日期：
          - 日期不在请求窗口内的丢弃（discover 的窗口过滤只认 region，仍可能漏进院线日期）
          - 用真实数字版日期覆盖 date / year
        只对将要推送的候选条目回查（limit 上限），避免无效请求。
        """
        confirmed: List[dict] = []
        checked = 0
        for item in items:
            if len(confirmed) >= limit:
                break
            checked += 1
            digital = self._digital_date(item["id"])
            window = self._window_of(item.get("phase"))
            if not digital:
                # 查不到数字版日期：丢弃，避免推送错误的院线日期
                logger.debug(f"流媒体上线监控：{item['key']} 无数字版日期，跳过")
                continue
            if window and not (window[0] <= digital <= window[1]):
                logger.debug(f"流媒体上线监控：{item['key']} 数字版日期 {digital} 不在窗口 "
                             f"{window[0]}~{window[1]}，跳过")
                continue
            item["date"] = digital
            item["year"] = digital[:4]
            confirmed.append(item)
        if len(confirmed) != checked:
            logger.info(f"流媒体上线监控：电影数字版日期校验通过 {len(confirmed)}/{checked} 条")
        return confirmed

    def _window_of(self, phase: Optional[str]) -> Optional[Tuple[str, str]]:
        """取某通道对应的日期窗口（YYYY-MM-DD, YYYY-MM-DD）"""
        today = self._now().date()
        if phase == "upcoming":
            return (today.strftime("%Y-%m-%d"),
                    (today + timedelta(days=self._days_ahead)).strftime("%Y-%m-%d"))
        if phase in ("recent", "airing"):
            return ((today - timedelta(days=self._days_back)).strftime("%Y-%m-%d"),
                    today.strftime("%Y-%m-%d"))
        return None

    def _digital_date(self, tmdb_id: Any) -> Optional[str]:
        """取该电影在本地区的数字版（type=4）发行日；缺失时回退到全球最早的数字版日期"""
        cache = self._digital_cache
        cache_key = (self._region, tmdb_id)
        if isinstance(cache, dict) and cache_key in cache:
            return cache[cache_key]

        data = self._tmdb_get(f"/movie/{tmdb_id}/release_dates", {})
        results = (data or {}).get("results") or []

        def pick(entry: dict) -> Optional[str]:
            dates = [rd.get("release_date") or "" for rd in entry.get("release_dates") or []
                     if rd.get("type") == 4]
            dates = sorted(d[:10] for d in dates if d)
            return dates[0] if dates else None

        found: Optional[str] = None
        for entry in results:
            if entry.get("iso_3166_1") == self._region:
                found = pick(entry)
                break
        if not found:
            # 本地区没有数字版记录时，回退到全球最早的数字版日期（仍比院线日期准确）
            candidates = [d for entry in results if (d := pick(entry))]
            found = min(candidates) if candidates else None
            if found:
                logger.debug(f"流媒体上线监控：movie:{tmdb_id} 本地区（{self._region}）无数字版记录，"
                             f"回退到全球最早数字版日期 {found}")

        if isinstance(cache, dict):
            cache[cache_key] = found
        return found

    def _build_item(self, raw: dict, mtype: str, phase: str) -> Optional[dict]:
        tmdb_id = raw.get("id")
        title = (raw.get("title") or raw.get("name") or "").strip()
        if not tmdb_id or not title:
            return None
        vote = self._to_float(raw.get("vote_average"), 0.0)
        if self._min_vote and vote < self._min_vote:
            return None
        first_date = raw.get("release_date") or raw.get("first_air_date") or ""
        return {
            "key": f"{mtype}:{tmdb_id}",
            "id": tmdb_id,
            "type": mtype,
            "title": title,
            "original_title": (raw.get("original_title") or raw.get("original_name") or "").strip(),
            "year": first_date[:4] if first_date else "",
            "date": first_date,
            "vote": round(vote, 1),
            "popularity": self._to_float(raw.get("popularity"), 0.0),
            "poster": self._image_url(raw.get("poster_path")),
            "overview": self._short((raw.get("overview") or "").strip(), 80),
            "phase": phase,
            "url": f"https://www.themoviedb.org/{'movie' if mtype == 'movie' else 'tv'}/{tmdb_id}",
            "providers": "",
        }

    def _attach_providers(self, items: List[dict]):
        """补充每条作品在本地区实际可用的平台（仅对最终待推送的条目查询）"""
        for item in items:
            # 国内平台条目的来源与平台已确定（B站/豆瓣），无需回查 TMDB
            if item.get("source") in ("bilibili", "douban"):
                continue
            try:
                data = self._tmdb_get(f"/{item['type']}/{item['id']}/watch/providers", {})
            except Exception as e:
                logger.debug(f"流媒体上线监控：获取平台信息失败（{item['key']}）：{e}")
                continue
            region_data = ((data or {}).get("results") or {}).get(self._region) or {}
            names: List[str] = []
            for bucket in ("flatrate", "free", "ads", "rent", "buy"):
                for provider in region_data.get(bucket) or []:
                    name = provider.get("provider_name")
                    if name and name not in names:
                        names.append(name)
                if names:
                    break
            item["providers"] = " / ".join(names[:4])

    def _tmdb_get(self, path: str, params: dict = None) -> Optional[dict]:
        """调用 TMDB API（复用 MoviePilot 的 Key / 域名 / 代理配置）"""
        api_key = getattr(settings, "TMDB_API_KEY", None)
        if not api_key:
            logger.error("流媒体上线监控：未配置 TMDB API Key，"
                         "请前往「设置 → 媒体识别 → TheMovieDb」配置")
            return None
        domain = getattr(settings, "TMDB_API_DOMAIN", None) or "api.themoviedb.org"
        base = domain.rstrip("/") if domain.startswith("http") else f"https://{domain.rstrip('/')}"
        query: Dict[str, Any] = {"api_key": api_key, "language": "zh-CN"}
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            query[key] = value
        res = RequestUtils(proxies=getattr(settings, "PROXY", None),
                           ua=getattr(settings, "USER_AGENT", None),
                           timeout=20).get_res(url=f"{base}/3{path}", params=query)
        if not res:
            logger.error(f"流媒体上线监控：请求 TMDB 失败：{path}")
            return None
        if res.status_code != 200:
            logger.error(f"流媒体上线监控：TMDB 返回 {res.status_code}：{path}")
            return None
        try:
            return res.json()
        except Exception as e:
            logger.error(f"流媒体上线监控：解析 TMDB 响应失败：{e}")
            return None

    # =====================================================================
    #  国内平台查询（B站放送表）
    # =====================================================================
    def _bili_items(self, failures: List[str]) -> List[dict]:
        """
        拉取 B站番剧 / 国创放送表，返回统一结构的条目列表。

        接口特征（实测）：
          * GET /pgc/web/timeline?types=<1番剧|4国创>&before=0&after=<0..7>
          * 无需任何鉴权；before 只能为 0，after 上限 7，即最多看未来 8 天。
          * result 是按日期分组的列表，每组含 episodes 数组：
              season_id  剧集 ID（去重键）
              title      剧名
              pub_index  集数文案（如「第24话」）
              pub_time   当日更新时刻（如「10:00」）
              pub_ts     该集上线的 Unix 时间戳
              published  1 = 已上线，0 = 待上线
              delay      1 = 延播（附 delay_reason）
              cover      封面图 URL
        同一 season 在窗口内可能出现多条（多集），只保留最早的一条。
        """
        types: List[Tuple[int, str]] = []
        if self._include_bili_anime:
            types.append((1, "番剧"))
        if self._include_bili_guochuang:
            types.append((4, "国创"))
        if not types:
            return []

        # 窗口跟随「即将上线窗口」，但受接口硬限制 after≤7 约束
        after = max(0, min(self._days_ahead, self._BILI_AFTER_MAX))
        picked: Dict[Any, dict] = {}
        for bili_type, kind in types:
            try:
                data = self._bili_get(self._BILI_TIMELINE, {
                    "types": bili_type, "before": 0, "after": after,
                })
            except Exception as e:
                logger.error(f"流媒体上线监控：B站{kind}放送表查询失败：{e}")
                failures.append(f"B站{kind}：{self._short(str(e), 60) or '未知错误'}")
                continue
            if not data:
                failures.append(f"B站{kind}：无响应")
                continue
            code = data.get("code")
            if code != 0:
                logger.error(f"流媒体上线监控：B站{kind}返回 code={code} "
                             f"{data.get('message')!r}")
                failures.append(f"B站{kind}：code={code} {data.get('message') or ''}".strip())
                continue
            count = 0
            for group in data.get("result") or []:
                for raw in group.get("episodes") or []:
                    item = self._bili_build_item(raw, kind, group.get("date"))
                    if not item:
                        continue
                    count += 1
                    # 窗口内同一剧集可能有多集（如周更番跨多天）。按 season_id
                    # 合并、只保留最早一集，避免一部番剧刷屏。
                    # 注意：item["key"] 仍带日期，跨次检测时「下一集」会作为新
                    # 键再次推送，不受此处窗口内合并的影响。
                    existed = picked.get(item["id"])
                    if existed is None or item["date"] < existed["date"]:
                        picked[item["id"]] = item
            logger.debug(f"流媒体上线监控：B站{kind}放送表命中 {count} 条")

        items = sorted(picked.values(), key=lambda item: (item["date"], item["title"]))
        return items

    def _bili_build_item(self, raw: dict, kind: str, group_date: Optional[str]) -> Optional[dict]:
        """把 B站放送表条目转成与 TMDB 一致的结构"""
        season_id = raw.get("season_id")
        title = (raw.get("title") or "").strip()
        if not season_id or not title:
            return None
        pub_ts = self._to_float(raw.get("pub_ts"), 0.0)
        if not pub_ts:
            return None
        # 延播条目的时刻不可信，直接剔除（避免推送错误时间）
        if raw.get("delay"):
            logger.debug(f"流媒体上线监控：B站《{title}》延播，跳过 "
                         f"（{raw.get('delay_reason') or '无说明'}）")
            return None
        moment = datetime.fromtimestamp(pub_ts)
        date_str = moment.strftime("%Y-%m-%d")
        # 待上线（published=0）与已上线（published=1）都属于窗口内有效条目
        return {
            # 键用「剧集 + 上线日」：同一剧集的后续集数会生成新键，可分别推送
            "key": f"bili:{season_id}:{date_str}",
            "id": season_id,
            "type": "tv",
            "source": "bilibili",
            "kind": kind,
            "title": title,
            "original_title": (raw.get("origin_title") or "").strip(),
            "year": date_str[:4],
            "date": date_str,
            "vote": 0.0,
            "popularity": self._to_float(raw.get("plays"), 0.0),
            "poster": (raw.get("cover") or raw.get("square_cover") or "").strip(),
            "overview": "",
            "phase": "upcoming",
            "episode": (raw.get("pub_index") or "").strip(),
            "pub_time": (raw.get("pub_time") or "").strip(),
            "published": bool(raw.get("published")),
            "follows": (raw.get("follows") or "").strip(),
            "url": f"https://www.bilibili.com/bangumi/play/ss{season_id}",
            "providers": "哔哩哔哩",
        }

    @staticmethod
    def _bili_get(url: str, params: dict = None) -> Optional[dict]:
        """请求 B站公开接口。国内站直连即可，无需代理与鉴权。"""
        res = RequestUtils(
            ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            referer="https://www.bilibili.com",
            timeout=20,
        ).get_res(url=url, params=params or {})
        if not res:
            raise RuntimeError("请求无响应")
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}")
        return res.json()

    # =====================================================================
    #  国内平台查询（豆瓣新上线）
    # =====================================================================
    # 每次检测每个集合最多回查的详情数：豆瓣详情接口需逐条请求，限制以控制
    # 请求量与反爬风险（实测低频请求 12 连发无封锁，但仍应克制）。
    _DOUBAN_DETAIL_LIMIT = 20

    def _douban_items(self, failures: List[str]) -> List[dict]:
        """
        拉取豆瓣「近期新上线」的国产剧 / 热门剧集 / 综艺。

        语义边界（实测结论，非缺陷）：
          * 豆瓣可用集合（tv_domestic/tv_hot/show_hot）的条目均为**已上线**，
            `pubdate` 是首播日期；`pre_playable_date` 全部为空。因此豆瓣通道
            只能提供「近期新上线」语义，无法提供「即将上线」的流媒体排播日期。
          * 电影侧集合（movie_latest）平台字段覆盖率为 0/20，故不纳入——电影
            已由 TMDB 的数字版日期通道覆盖，无需重复。
          * tv_hot 与 tv_domestic 存在大量重合条目，必须按豆瓣 id 跨集合去重。

        请求策略：
          * 列表接口含 `vendor_icons`（可判断该条目有无平台信息），但**不含**
            `pubdate`。故先用列表响应预筛「有平台」的条目，再逐条回查详情取
            上线日期与平台名，避免对无平台条目浪费请求。
        """
        collections = list(self._DOUBAN_COLLECTIONS)
        if not self._include_douban_show:
            # 关闭综艺后只保留剧集类集合
            collections = [(cid, name) for cid, name in collections if cid != "show_hot"]
        if not collections:
            return []

        today = self._now().date()
        lo = today - timedelta(days=self._days_back)
        picked: Dict[Any, dict] = {}

        for cid, kind in collections:
            try:
                data = self._douban_get(f"/subject_collection/{cid}/items",
                                        {"start": 0, "count": self._DOUBAN_DETAIL_LIMIT})
            except Exception as e:
                logger.error(f"流媒体上线监控：豆瓣{kind}列表查询失败：{e}")
                failures.append(f"豆瓣{kind}：{self._short(str(e), 60) or '未知错误'}")
                continue
            if not data:
                failures.append(f"豆瓣{kind}：无响应")
                continue

            listed = data.get("subject_collection_items") or []
            # 预筛：只回查列表里已标注平台的条目（vendor_icons 非空）
            candidates = [row for row in listed if row.get("vendor_icons")]
            skipped = len(listed) - len(candidates)
            logger.debug(f"流媒体上线监控：豆瓣{kind}列表 {len(listed)} 条，"
                         f"有平台标记 {len(candidates)} 条，跳过 {skipped} 条")

            count = 0
            for row in candidates:
                item = self._douban_build_item(row, kind, lo, today)
                if not item:
                    continue
                count += 1
                # 跨集合去重：同一部剧可能同时出现在 tv_hot 与 tv_domestic
                existed = picked.get(item["key"])
                if existed is None or item["date"] > existed["date"]:
                    picked[item["key"]] = item
            logger.debug(f"流媒体上线监控：豆瓣{kind}详情回查命中 {count} 条")

        items = sorted(picked.values(), key=lambda item: (item["date"], item["title"]),
                       reverse=True)
        return items

    def _douban_build_item(self, row: dict, kind: str, lo: date, hi: date) -> Optional[dict]:
        """回查豆瓣详情，取上线日期与平台名，并按窗口过滤"""
        douban_id = row.get("id")
        subject_type = (row.get("type") or "tv").strip() or "tv"
        if not douban_id:
            return None
        try:
            detail = self._douban_get(f"/{subject_type}/{douban_id}",
                                      {"for_mobile": 1})
        except Exception as e:
            logger.debug(f"流媒体上线监控：豆瓣条目 {douban_id} 详情查询失败：{e}")
            return None
        if not detail or not isinstance(detail, dict):
            return None

        title = (detail.get("title") or row.get("title") or "").strip()
        if not title:
            return None
        # 平台名：优先 linewatches[].source.name（干净可读），回退 vendors 的 bundle id
        platforms = self._douban_platforms(detail)
        if not platforms:
            return None
        # 上线日期：pubdate 是 list[str]，形如「2026-09-11(中国大陆)」
        released = self._douban_pubdate(detail)
        if not released or released < lo or released > hi:
            return None

        pic = (detail.get("pic") or row.get("pic") or {})
        poster = (pic.get("large") or pic.get("normal") or "") if isinstance(pic, dict) else ""
        rating = detail.get("rating") or {}
        vote = self._to_float(rating.get("value"), 0.0) if isinstance(rating, dict) else 0.0
        return {
            "key": f"douban:{douban_id}",
            "id": douban_id,
            "type": "tv",
            "source": "douban",
            "kind": kind,
            "title": title,
            "original_title": (detail.get("original_title") or "").strip(),
            "year": (detail.get("year") or row.get("year") or released.year),
            "date": released.strftime("%Y-%m-%d"),
            "vote": round(vote, 1),
            "popularity": self._to_float(row.get("wish_count"), 0.0),
            "poster": poster,
            "overview": self._short((detail.get("intro") or "").strip(), 80),
            "phase": "recent",
            "episode": (detail.get("episodes_info") or "").strip(),
            "url": f"https://movie.douban.com/subject/{douban_id}/",
            "providers": " / ".join(platforms[:4]),
        }

    def _douban_platforms(self, detail: dict) -> List[str]:
        """从豆瓣详情里抽取干净的平台名列表"""
        names: List[str] = []
        for line in detail.get("linewatches") or []:
            source = line.get("source") or {}
            name = (source.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
        if names:
            return names
        # 回退：用 vendors 的 app_bundle_id 反查平台名
        for vendor in detail.get("vendors") or []:
            bundle = (vendor.get("app_bundle_id") or "").lower()
            for key, name in self._DOUBAN_BUNDLE_MAP.items():
                if key in bundle and name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _douban_pubdate(detail: dict) -> Optional[date]:
        """解析 pubdate（形如 ['2026-09-11(中国大陆)']）取第一个有效日期"""
        for entry in detail.get("pubdate") or []:
            match = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(entry))
            if not match:
                continue
            try:
                return datetime.strptime(match.group(0), "%Y-%m-%d").date()
            except ValueError:
                continue
        return None

    def _douban_get(self, path: str, params: dict = None) -> Optional[dict]:
        """请求豆瓣 rexxar 移动端接口。无需鉴权，但需 Referer 与移动端 UA。"""
        res = RequestUtils(
            ua=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"),
            referer="https://m.douban.com/",
            timeout=20,
        ).get_res(url=f"{self._DOUBAN_API}{path}", params=params or {})
        if not res:
            raise RuntimeError("请求无响应")
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}")
        return res.json()

    # =====================================================================
    #  平台名称解析
    # =====================================================================
    def _resolve_provider_ids(self) -> Tuple[List[int], List[str]]:
        """把配置中的平台名称/ID 解析为 TMDB 平台 ID"""
        raw = (self._providers or "").replace("，", ",").replace("、", ",").replace(";", ",")
        tokens = [token.strip() for token in raw.split(",") if token.strip()]
        if not tokens:
            return [], []
        providers = self._load_providers()
        ids: List[int] = []
        unknown: List[str] = []
        for token in tokens:
            if token.isdigit():
                ids.append(int(token))
                continue
            provider_id = self._match_provider(token, providers)
            if provider_id:
                ids.append(provider_id)
            else:
                unknown.append(token)
        if unknown:
            logger.warning(f"流媒体上线监控：未识别的平台 {unknown}，"
                           f"该地区可用平台：{self._available_provider_names()}")
        return list(dict.fromkeys(ids)), unknown

    def _load_providers(self) -> List[dict]:
        """获取并缓存指定地区的 TMDB 平台清单（中英文名都参与匹配）"""
        cache_key = f"{self._KEY_PROVIDERS}_{self._region}"
        cached = self.get_data(cache_key)
        if not isinstance(cached, dict):
            cached = {}
        items = cached.get("items") or []
        timestamp = self._to_float(cached.get("ts"), 0.0)
        if items and timestamp and (time.time() - timestamp) < self._PROVIDER_CACHE_TTL:
            return items

        merged: Dict[int, dict] = {}
        for language in ("zh-CN", "en-US"):
            for path in ("/watch/providers/movie", "/watch/providers/tv"):
                data = self._tmdb_get(path, {"watch_region": self._region, "language": language})
                for provider in (data or {}).get("results") or []:
                    pid = provider.get("provider_id")
                    name = (provider.get("provider_name") or "").strip()
                    if not pid or not name:
                        continue
                    entry = merged.setdefault(int(pid), {"id": int(pid), "names": []})
                    if name not in entry["names"]:
                        entry["names"].append(name)
        items = [entry for entry in merged.values() if entry["names"]]
        if items:
            self.save_data(cache_key, {"ts": time.time(), "items": items, "region": self._region})
            logger.info(f"流媒体上线监控：已更新 {self._region} 地区平台清单，共 {len(items)} 个")
        else:
            # 该地区在 TMDB 没有任何平台数据（例如 CN）：明确告警，不要静默当作“无结果”
            logger.warning(f"流媒体上线监控：TMDB 未收录 {self._region} 地区的流媒体平台数据，"
                           "按平台筛选会得到空结果；建议改用 HK / TW / US / JP / KR / GB / SG")
        return items

    def _available_provider_names(self) -> List[str]:
        names: List[str] = []
        for entry in self._load_providers():
            name = entry["names"][0]
            if name not in names:
                names.append(name)
        return names

    @classmethod
    def _match_provider(cls, token: str, providers: List[dict]) -> Optional[int]:
        """按平台名称匹配 TMDB 平台 ID（支持中英文、忽略空格与符号）"""
        target = cls._norm(token)
        if not target:
            return None
        for entry in providers:
            for name in entry["names"]:
                if cls._norm(name) == target:
                    return entry["id"]
        if len(target) < 2:
            return None
        for entry in providers:
            for name in entry["names"]:
                normalized = cls._norm(name)
                if normalized and (target in normalized or normalized in target):
                    return entry["id"]
        return None

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[\s+\-_.&'’（）()【】\[\]/]+", "", (text or "").lower())

    # =====================================================================
    #  工具方法
    # =====================================================================
    @staticmethod
    def _image_url(path: Optional[str]) -> str:
        if not path:
            return ""
        domain = getattr(settings, "TMDB_IMAGE_DOMAIN", None) or "image.tmdb.org"
        base = domain.rstrip("/") if domain.startswith("http") else f"https://{domain.rstrip('/')}"
        return f"{base}/t/p/w500{path}"

    @staticmethod
    def _short(text: str, length: int) -> str:
        text = re.sub(r"\s+", " ", text or "")
        return text if len(text) <= length else text[:length] + "…"

    @staticmethod
    def _tz():
        name = getattr(settings, "TZ", None) or "Asia/Shanghai"
        try:
            return pytz.timezone(name)
        except Exception:
            return pytz.timezone("Asia/Shanghai")

    def _now(self) -> datetime:
        return datetime.now(self._tz())

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    # =====================================================================
    #  详情页
    # =====================================================================
    @staticmethod
    def _item_kind_label(item: dict) -> str:
        """条目的类型标签：区分 TMDB 电影/剧集 与 国内平台的番剧/国创/新上线"""
        if item.get("source") == "bilibili":
            return f"B站·{item.get('kind') or '剧集'}"
        if item.get("source") == "douban":
            return f"豆瓣·{item.get('kind') or '剧集'}"
        return "电影" if item.get("type") == "movie" else "剧集"

    @staticmethod
    def _item_link_label(item: dict) -> str:
        """条目的外链按钮文案：不同数据源指向不同站点"""
        if item.get("source") == "bilibili":
            return "哔哩哔哩"
        if item.get("source") == "douban":
            return "豆瓣"
        return "TMDB"

    def get_page(self) -> List[dict]:
        """插件详情页：概览 + 最新结果 + 推送历史"""
        latest = self.get_data(self._KEY_LATEST)
        if not isinstance(latest, dict):
            latest = {}
        history = self.get_data(self._KEY_HISTORY)
        if not isinstance(history, list):
            history = []
        items = latest.get("items") or []
        if not isinstance(items, list):
            items = []

        summary = (
            f"插件状态：{'已启用' if self._enabled else '未启用'}　|　"
            f"监控区域：{self._region}　|　监控平台：{self._providers or '该地区全部平台'}　|　"
            f"检测计划：{self._cron}　|　最近检测：{latest.get('time') or '尚未检测'}"
            f"（命中 {latest.get('count', 0)} 条）"
        )
        page: List[dict] = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {"component": "VAlert",
                             "props": {"type": "info" if self._enabled else "warning",
                                       "variant": "tonal", "class": "text-pre-wrap",
                                       "text": summary}}
                        ],
                    }
                ],
            }
        ]

        if not items:
            page.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {"component": "VAlert",
                             "props": {"type": "info", "variant": "tonal",
                                       "text": "暂无检测结果。可在插件配置页勾选「立即运行一次」，"
                                               "或等待下一个定时任务触发。"}}
                        ],
                    }
                ],
            })
            if history:
                page.append(self._history_table(history))
            return page

        # 海报卡片
        cards = []
        for item in items[:12]:
            cards.append(self._item_card(item))
        if cards:
            page.append({"component": "VRow", "content": cards})

        # 明细表格
        headers = ["类型", "标题", "年份", "日期", "评分", "上线平台"]
        table_rows = []
        for item in items:
            cells = [
                self._td(self._item_kind_label(item)),
                self._td(f"《{item.get('title')}》"),
                self._td(item.get("year") or "-"),
                self._td(item.get("date") or "近期更新"),
                self._td(f"{item.get('vote')}" if item.get("vote") else "-"),
                self._td(item.get("providers") or "-"),
            ]
            table_rows.append({
                "component": "tr",
                "props": {"class": "text-sm"},
                "content": cells + [{
                    "component": "td",
                    "content": [{
                        "component": "a",
                        "props": {"href": item.get("url"), "target": "_blank"},
                        "text": self._item_link_label(item),
                    }],
                }],
            })
        page.append({
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "VTable",
                            "props": {"hover": True},
                            "content": [
                                {
                                    "component": "thead",
                                    "content": [self._th(text) for text in headers + ["链接"]],
                                },
                                {"component": "tbody", "content": table_rows},
                            ],
                        }
                    ],
                }
            ],
        })

        if history:
            page.append(self._history_table(history))
        return page

    def _item_card(self, item: dict) -> dict:
        content: List[dict] = []
        if item.get("poster"):
            content.append({"component": "VImg",
                            "props": {"src": item["poster"], "height": "260", "cover": True}})
        lines = [f"《{item.get('title')}》"]
        meta = " · ".join(part for part in [
            self._item_kind_label(item),
            item.get("year") or "",
            f"{item.get('vote')}分" if item.get("vote") else "",
        ] if part)
        lines.append(meta)
        # 国内平台条目展示集数与上线信息，TMDB 条目展示数字版/首播日期
        if item.get("source") == "bilibili":
            if item.get("episode"):
                lines.append(f"{item['episode']}　更新 {item.get('date') or '-'}"
                             f" {item.get('pub_time') or ''}".rstrip())
        elif item.get("source") == "douban":
            detail = "　".join(part for part in [
                item.get("episode") or "",
                f"上线 {item['date']}" if item.get("date") else "",
            ] if part)
            if detail:
                lines.append(detail)
        elif item.get("date"):
            lines.append(f"{'数字版' if item.get('type') == 'movie' else '首播'} {item['date']}")
        elif item.get("phase") == "airing":
            lines.append("近期有新集")
        if item.get("providers"):
            lines.append(item["providers"])
        if item.get("overview"):
            lines.append(item["overview"])
        content.append({
            "component": "VCardText",
            "content": [{"component": "div", "props": {"class": "text-body-2"}, "text": line}
                        for line in lines],
        })
        content.append({
            "component": "VCardActions",
            "content": [{
                "component": "VBtn",
                "props": {"variant": "text", "size": "small",
                          "href": item.get("url"), "target": "_blank"},
                "text": self._item_link_label(item),
            }],
        })
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6, "md": 4, "lg": 3},
            "content": [{
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": content,
            }],
        }

    def _history_table(self, history: List[dict]) -> dict:
        rows = []
        for record in history:
            rows.append({
                "component": "tr",
                "props": {"class": "text-sm"},
                "content": [
                    self._td(record.get("time") or "-"),
                    self._td(str(record.get("count", 0))),
                    self._td(record.get("titles") or "-"),
                ],
            })
        return {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {"component": "div", "props": {"class": "text-h6 pa-2"},
                         "text": "推送历史"},
                        {
                            "component": "VTable",
                            "props": {"hover": True},
                            "content": [
                                {"component": "thead",
                                 "content": [self._th(text) for text in ["时间", "条数", "内容"]]},
                                {"component": "tbody", "content": rows},
                            ],
                        },
                    ],
                }
            ],
        }

    @staticmethod
    def _th(text: str) -> dict:
        return {"component": "th", "props": {"class": "text-start"}, "text": text}

    @staticmethod
    def _td(text: str) -> dict:
        return {"component": "td", "text": text}
