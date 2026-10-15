"""
MoviePolit - 流媒体上线监控

监控指定地区流媒体平台（爱奇艺 / 腾讯视频 / 优酷 / 芒果TV / 哔哩哔哩 / Netflix / Disney+ ...）
即将上线与近期上线的电影、剧集，通过 MoviePilot 已启用的通知渠道推送。

数据来源：TheMovieDb（复用 MoviePilot「设置 → 媒体识别 → TheMovieDb」中的 API Key、
API 域名、图片域名与代理配置，无需额外填写任何密钥）。
"""

import re
import time
from datetime import datetime, timedelta
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
    plugin_desc = "监控指定地区流媒体平台即将上线 / 近期上线的电影、剧集，通过 MoviePilot 通知渠道推送。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.jpg"
    plugin_version = "1.0.0"
    plugin_author = "MoviePolit"
    plugin_config_prefix = "moviepolit_"
    plugin_order = 21
    auth_level = 1

    # ===== 默认配置（类属性兜底，保证 get_service 在 init_plugin 之前也有值）=====
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "0 9 * * *"
    _region: str = "CN"
    _providers: str = "爱奇艺,腾讯视频,优酷,芒果TV,哔哩哔哩"
    _include_movie: bool = True
    _include_tv: bool = True
    _include_tv_airing: bool = True
    _strict_digital: bool = True
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

    # ===== 插件数据 key =====
    _KEY_LATEST = "latest"
    _KEY_PUSHED = "pushed"
    _KEY_HISTORY = "history"
    _KEY_PROVIDERS = "providers"

    # 去重记录保留天数
    _DEDUPE_KEEP_DAYS = 180
    # 平台名称缓存有效期（秒）
    _PROVIDER_CACHE_TTL = 7 * 24 * 3600

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
        self._region = (config.get("region") or "CN").strip().upper()
        self._providers = (config.get("providers") or "").strip()
        self._include_movie = bool(config.get("include_movie", True))
        self._include_tv = bool(config.get("include_tv", True))
        self._include_tv_airing = bool(config.get("include_tv_airing", True))
        self._strict_digital = bool(config.get("strict_digital", True))
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
                                               "placeholder": "爱奇艺,腾讯视频,优酷,芒果TV,哔哩哔哩"}}
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
                    # 第五行：时间窗口与条数
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
                    # 第六行：过滤条件
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
                    # 第七行：通知细节
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
                                                 "「设置 → 通知」启用渠道。平台名称支持中文名（爱奇艺）、英文名"
                                                 "（iQIYI）或 TMDB 平台 ID；识别失败的平台会在通知里提示。"
                                                 "即将上线的作品 TMDB 可能尚未关联平台数据，若结果偏少可把「监控平台」留空。",
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
            "region": "CN",
            "providers": "爱奇艺,腾讯视频,优酷,芒果TV,哔哩哔哩",
            "include_movie": True,
            "include_tv": True,
            "include_tv_airing": True,
            "strict_digital": True,
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
        if not passes:
            logger.warning("流媒体上线监控：未勾选电影和剧集，跳过本次检测")
            return

        # 查询
        grouped: Dict[Tuple[str, str], List[dict]] = {}
        seen = set()
        failures: List[str] = []
        for mtype, phase in passes:
            try:
                found = self._discover(mtype, phase, provider_ids)
            except Exception as e:
                logger.error(f"流媒体上线监控：查询失败（{mtype}/{phase}）：{e}")
                failures.append(f"{self._phase_label(mtype, phase)}")
                continue
            bucket = grouped.setdefault((mtype, phase), [])
            for item in found:
                if item["key"] in seen:
                    continue
                seen.add(item["key"])
                bucket.append(item)

        # 去重已推送 + 配额裁剪
        limit = self._max_items if self._max_items > 0 else 100
        quota = max(1, int(limit / len(passes)) + (1 if limit % len(passes) else 0))
        pushed = self.get_data(self._KEY_PUSHED)
        if not isinstance(pushed, dict):
            pushed = {}
        sections: List[Tuple[str, List[dict]]] = []
        total = 0
        for mtype, phase in passes:
            candidates = grouped.get((mtype, phase)) or []
            if self._dedupe:
                candidates = [item for item in candidates if item["key"] not in pushed]
            candidates = candidates[:quota]
            if candidates:
                sections.append((self._phase_label(mtype, phase), candidates))
                total += len(candidates)

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
            if failures:
                # 全部通道失败（如未配置 TMDB Key、网络不通）时明确告警，避免静默
                logger.error(f"流媒体上线监控：检测失败：{failures}")
                try:
                    self.post_message(mtype=self._notification_type(),
                                      title="🎬 流媒体上线监控：检测失败",
                                      text="本次检测未能获取到数据：\n"
                                           + "\n".join(f"· {item}" for item in failures))
                except Exception as e:
                    logger.error(f"流媒体上线监控：发送失败通知出错：{e}")
            else:
                logger.info("流媒体上线监控：本次没有新的可推送内容")
            return

        # 推送
        title = f"🎬 流媒体上线速递 · {self._region}"
        text = self._build_message(sections, unknown, failures)
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

    def _build_message(self, sections: List[Tuple[str, List[dict]]],
                       unknown: List[str], failures: List[str]) -> str:
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
        lines.append(f"共 {total} 条 · 数据来源 TMDB · 点击消息查看完整详情")
        if unknown:
            available = self._available_provider_names()
            lines.append("⚠️ 未识别的平台：" + "、".join(unknown)
                         + (f"（该地区可用：{'、'.join(available[:12])}）" if available else ""))
        if failures:
            lines.append("⚠️ 部分查询失败：" + "、".join(failures))
        return "\n".join(lines)

    def _format_line(self, item: dict) -> str:
        parts = [f"· 《{item['title']}》"]
        if item.get("year"):
            parts.append(item["year"])
        if item.get("vote"):
            parts.append(f"{item['vote']}分")
        if item.get("phase") == "airing":
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
            if phase == "upcoming":
                params["release_date.gte"] = today.strftime("%Y-%m-%d")
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
                params["first_air_date.gte"] = today.strftime("%Y-%m-%d")
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
                self._td("电影" if item.get("type") == "movie" else "剧集"),
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
                        "text": "TMDB",
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
            "电影" if item.get("type") == "movie" else "剧集",
            item.get("year") or "",
            f"{item.get('vote')}分" if item.get("vote") else "",
        ] if part)
        lines.append(meta)
        if item.get("date"):
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
                "text": "TMDB",
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
