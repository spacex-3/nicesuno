# encoding:utf-8
import os
import re
import json
import time
import requests
import plugins
import threading
import traceback
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.expired_dict import ExpiredDict
from common.log import logger
from plugins import *
from channel.chat_message import ChatMessage
from typing import List
from typing import Tuple
from config import conf
from lib import itchat
from lib.itchat.content import *
from pathvalidate import sanitize_filename
from .ctext import *


@plugins.register(
    name="Nicesuno",
    desire_priority=90,
    hidden=False,
    desc="一款基于Suno和中转API创作音乐的插件。",
    version="1.4",
    author="SpaceX",
)
class Nicesuno(Plugin):
    def __init__(self):
        super().__init__()
        self.trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        self.help_text = self._generate_help_text()
        
        try:
            
            # 默认配置
            gconf = {
                "suno_api_bases": [],
                "suno_admin_password": "",
                "music_create_prefixes": [],
                "instrumental_create_prefixes": [],
                "lyrics_create_prefixes": [],
                "music_output_dir": "/tmp",
                "is_send_lyrics": True,
                "is_send_covers": True,
                "suno_api_token": "",
                "http_headers": {
                    "Content-Type": "application/json"
                }
            }

            # 配置文件路径
            curdir = os.path.dirname(__file__)
            self.json_path = os.path.join(curdir, "config.json")
            self.roll_path = os.path.join(curdir, "user_info.pkl")
            self.user_datas_path = os.path.join(curdir, "user_datas.pkl")
            tm_path = os.path.join(curdir, "config.json.template")



            # 环境变量加载
            env = {}
            for key in gconf.keys():
                if os.environ.get(key, None):
                    env[key] = os.environ.get(key)
                    break
            # 加载配置文件或模板
            jld = {}
            if os.path.exists(self.json_path):
                jld = json.loads(read_file(self.json_path))
            elif os.path.exists(tm_path):
                jld = json.loads(read_file(tm_path))

            # 合并配置（默认配置 -> 配置文件 -> 环境变量）
            gconf = {**gconf, **jld, **env}

            # 动态生成 Authorization 头部信息
            gconf['http_headers']['Authorization'] = f'Bearer {gconf.get("suno_api_token", "")}'

            # 处理管理员密码
            if gconf["suno_admin_password"] == "":
                self.temp_password = "12345678"
                logger.info("[suno] 因未设置管理员密码，本次的临时密码为%s。" % self.temp_password)
            else:
                self.temp_password = None

            # 处理前缀列表配置项
            for key, value in gconf.items():
                if key.endswith("_prefixes"):
                    gconf[key] = eval(value) if isinstance(value, str) else value

            # 存储配置到类属性
            self.config = gconf
            self.suno_api_bases = gconf.get("suno_api_bases", [])
            self.music_create_prefixes = gconf.get("music_create_prefixes", [])
            self.instrumental_create_prefixes = gconf.get("instrumental_create_prefixes", [])
            self.lyrics_create_prefixes = gconf.get("lyrics_create_prefixes", [])
            self.music_output_dir = gconf.get("music_output_dir", "/tmp")
            self.is_send_lyrics = gconf.get("is_send_lyrics", True)
            self.is_send_covers = gconf.get("is_send_covers", True)
            self.http_headers = gconf['http_headers']

            # 确保音乐输出目录存在
            if not os.path.exists(self.music_output_dir):
                logger.info(f"[Nicesuno] music_output_dir={self.music_output_dir} not exists, create it.")
                os.makedirs(self.music_output_dir)

            # 校验和初始化插件
            if self.suno_api_bases and isinstance(self.suno_api_bases, List) \
                    and self.music_create_prefixes and isinstance(self.music_create_prefixes, List):
                self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
                logger.info("[Nicesuno] inited")
            else:
                logger.warn("[Nicesuno] init failed because suno_api_bases or music_create_prefixes is incorrect.")

            # 设置初始 Suno API base
            self.suno_api_base = self.suno_api_bases[0] if self.suno_api_bases else None

            self.config = gconf

            logger.info("[suno] config={}".format(self.config))
            
            # 重新写入合并后的配置文件
            write_file(self.json_path, self.config)

            # 初始化用户数据
            self.roll = {
                "suno_admin_users": [],
                "suno_groups": [],
                "suno_users": [],
                "suno_bgroups": [],
                "suno_busers": []
            }
            if os.path.exists(self.roll_path):
                sroll = read_pickle(self.roll_path)
                self.roll = {**self.roll, **sroll}

            # 写入用户列表
            write_pickle(self.roll_path, self.roll)

            # 初始化用户数据
            self.user_datas = {}
            if os.path.exists(self.user_datas_path):
                self.user_datas = read_pickle(self.user_datas_path)
                logger.debug(f"[Nicesuno] Loaded user_datas: {self.user_datas}")
                

            # 会话管理
            if conf().get("expires_in_seconds"):
                self.sessions = ExpiredDict(conf().get("expires_in_seconds"))
            else:
                self.sessions = dict()

            self.issuno = True  # 机器人是否运行中
            logger.info("[Nicesuno] inited successfully")

        except Exception as e:
            logger.error(f"[Nicesuno] init failed, ignored.")
            raise e

    def get_help_text(self, **kwargs):
        # 获取用户的剩余使用次数
        remaining_uses = self.userInfo.get('limit', '未知')

        # 生成普通用户的帮助文本
        help_text = f"使用Suno创作音乐。\n今日剩余使用次数：{remaining_uses}\n\n1.创作声乐\n用法：唱/演唱<提示词>\n示例：唱明天会更好。\n\n2.创作器乐\n用法：演奏<提示词>\n示例：演奏明天会更好。\n\n3.自定义模式\n用法：\n唱/演唱/演奏\n标题: <标题>\n风格: <风格1> <风格2> ...\n<歌词>\n备注：前三行必须为创作前缀、标题、风格，<标题><风格><歌词>三个值可以为空，但<风格><歌词>不可同时为空！\n\n注意，使用本插件请避免政治、色情、名人等相关提示词，监测到则可能存在停止使用风险。"
        
        # 如果是管理员，附加管理员指令的帮助信息
        if kwargs.get("admin", False) is True:
            help_text += "\n\n管理员指令：\n"
            for cmd, info in ADMIN_COMMANDS.items():
                alias = [self.trigger_prefix + a for a in info["alias"][:1]]
                help_text += f"{','.join(alias)} "
                if "args" in info:
                    args = [a for a in info["args"]]
                    help_text += f"{' '.join(args)}"
                help_text += f": {info['desc']}\n"
        
        return help_text



    def _generate_help_text(self):
        help_text = "欢迎使用Suno音乐创作插件\n"
        help_text += "这是一个基于AI的音乐创作工具，通过输入文本提示生成对应的音乐作品。\n"
        help_text += "-----------------------------\n"
        help_text += "🎵 插件使用说明:\n"
        help_text += f"(1) 唱歌创作: 输入 ['{self.trigger_prefix}suno + 歌词提示'] 生成带歌词的音乐\n"
        help_text += f"(2) 器乐创作: 输入 ['{self.trigger_prefix}演奏 + 器乐提示'] 生成纯器乐音乐\n"
        help_text += f"(3) 自定义模式: 使用以下格式生成音乐:\n"
        help_text += f"    标题: <标题>\n"
        help_text += f"    风格: <风格1> <风格2> ...\n"
        help_text += f"    歌词: <歌词>\n"
        help_text += f"    示例: {self.trigger_prefix}suno 标题: 明天会更好\n    风格: 流行\n    歌词: 明天会更好\n"
        help_text += "    注意: 标题、风格、歌词三个值可以为空，但风格和歌词不可同时为空！\n"
        help_text += "-----------------------------\n"
        help_text += "📜 其他指令说明:\n"
        help_text += f"(1) 管理员指令: 使用 ['{self.trigger_prefix}suno_admin_cmd'] 查看管理员可用的指令\n"
        help_text += f"(2) 查询使用次数: 使用 ['{self.trigger_prefix}suno_g_info'] 查询当前用户的剩余创作次数\n"
        help_text += f"(3) 帮助文档: 使用 ['{self.trigger_prefix}suno_help'] 查看本帮助文档\n"
        help_text += "-----------------------------\n"
        help_text += "⚠️ 注意事项:\n"
        help_text += "1. 请避免输入政治、色情、名人等敏感词汇，否则可能导致生成失败。\n"
        help_text += "2. 每日创作次数有限，请合理使用。本系统设定的每日创作次数限制为：{daily_limit} 次。\n"
        help_text += "3. 创作失败时，将返还本次消耗的次数。\n"
        return help_text


    def on_handle_context(self, e_context: EventContext):
        try:
            if not isinstance(self.user_datas, dict):
                logger.error(f"Expected self.user_datas to be a dictionary, but got {type(self.user_datas)}")
        
            # 判断是否是TEXT类型消息
            if e_context["context"].type not in [
                ContextType.TEXT,
            ]:
                return
            context = e_context["context"]
            content = context.content
            logger.debug(f"[Nicesuno] on_handle_context. content={content}")
            self.sessionid = context["session_id"]
            logger.debug(f"[Nicesuno] sessionid: {self.sessionid}")
            self.userInfo = self.get_user_info(e_context)
            if not isinstance(self.userInfo, dict):
                logger.error(f"Expected self.userInfo to be a dictionary, but got {type(self.userInfo)}")
            logger.debug(f"[Nicesuno] userInfo: {self.userInfo}")
            self.isgroup = self.userInfo["isgroup"]
            logger.debug(f"[Nicesuno] isgroup: {self.isgroup}")

            if ContextType.TEXT == context.type and content.startswith(self.trigger_prefix):
                return self.handle_command(e_context)

            # 拦截非白名单黑名单群组
            if not self.userInfo["isadmin"] and self.isgroup and not self.userInfo["iswgroup"] and self.userInfo["isbgroup"]:
                logger.debug("[Nicesuno] Blocked by group whitelist/blacklist.")
                return
        
            # 拦截黑名单用户
            if not self.userInfo["isadmin"] and self.userInfo["isbuser"]:
                logger.debug("[Nicesuno] Blocked by user blacklist.")
                return
            
            # 判断是否包含创作的前缀
            make_instrumental, make_lyrics = False, False
            music_create_prefix = self._check_prefix(content, self.music_create_prefixes)
            instrumental_create_prefix = self._check_prefix(content, self.instrumental_create_prefixes)
            lyrics_create_prefix = self._check_prefix(content, self.lyrics_create_prefixes)
            if music_create_prefix:
                suno_prompt = content[len(music_create_prefix):].strip()
            elif instrumental_create_prefix:
                make_instrumental = True
                suno_prompt = content[len(instrumental_create_prefix):].strip()
            elif lyrics_create_prefix:
                make_lyrics = True
                suno_prompt = content[len(lyrics_create_prefix):].strip()
            else:
                logger.debug(f"[Nicesuno] content starts without any suno prefixes, ignored.")
                return

            # 判断是否包含创作的提示词
            if not suno_prompt:
                logger.info("[Nicesuno] content starts without any suno prompts, ignored.")
                return

            # 开始创作
            if make_lyrics:
                logger.info(f"[Nicesuno] start generating lyrics, suno_prompt={suno_prompt}.")
                self._create_lyrics(e_context, suno_prompt)
            else:
                logger.info(
                    f"[Nicesuno] start generating {'instrumental' if make_instrumental else 'vocal'} music, suno_prompt={suno_prompt}.")
                self._create_music(e_context, suno_prompt, make_instrumental)
        except Exception as e:
            logger.warning(f"[Nicesuno] failed to generate music, error={e}")
            logger.warning(f"Traceback: {traceback.format_exc()}")
            reply = Reply(ReplyType.TEXT, "抱歉！创作失败了，请稍后再试🥺")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    # 创作音乐
    def _create_music(self, e_context, suno_prompt, make_instrumental=False):
        custom_mode = False
        env = env_detection(self, e_context)
        if not env:
            return
        # 自定义模式
        if '标题' in suno_prompt and '风格' in suno_prompt:
            regex_prompt = r' *标题[:：]?(?P<title>[\S ]*)\n+ *风格[:：]?(?P<tags>[\S ]*)(\n+(?P<lyrics>.*))?'
            r = re.fullmatch(regex_prompt, suno_prompt, re.DOTALL)
            title = r.group('title').strip() if r and r.group('title') else None
            tags = r.group('tags').strip() if r and r.group('tags') else None
            lyrics = r.group('lyrics').strip() if r and r.group('lyrics') else None
            if r and (tags or lyrics):
                custom_mode = True
                logger.info(f"[Nicesuno] generating {'instrumental' if make_instrumental else 'vocal'} music in custom mode, title={title}, tags={tags}, lyrics={lyrics}")
                data = self._suno_generate_music_custom_mode(title, tags, lyrics, make_instrumental)
            else:
                logger.warning(f"[Nicesuno] generating {'instrumental' if make_instrumental else 'vocal'} music in custom mode failed because of wrong format, suno_prompt={suno_prompt}")
                reply = Reply(ReplyType.TEXT, self.get_help_text())
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
        # 描述模式
        else:
            logger.info(f"[Nicesuno] generating {'instrumental' if make_instrumental else 'vocal'} music with description, description={suno_prompt}")
            data = self._suno_generate_music_with_description(suno_prompt, make_instrumental)

        channel = e_context["channel"]
        context = e_context["context"]
        to_user_nickname = context["msg"].to_user_nickname
        if not data:
            logger.warning(f"[Nicesuno] no task_id in response data, response data={data}")
            reply = Reply(ReplyType.TEXT, f"因为神秘原因，创作失败了😂请稍后再试...")
        else: # 获取和发送音乐
            # 从 data 中提取 task_id，并确保只传递 task_id 本身
            task_id = data['data']  # 提取 task_id
            aids = [task_id]  # 这里传递的是 task_id
            logger.debug(f"[Nicesuno] start to handle music, aids={aids}, data={data}")
            threading.Thread(target=self._handle_music, args=(channel, context, task_id)).start()
            
            # 获取用户当前剩余次数
            remaining_uses = self.user_datas[self.userInfo['user_id']]["suno_data"]["limit"]

            # 生成回复消息
            reply = Reply(ReplyType.TEXT, f"{to_user_nickname}正在为您创作音乐，大约2分钟，请您稍等☕ 本次生成音乐后，今日还剩余{remaining_uses}次。")

        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    # 创作歌词
    def _create_lyrics(self, e_context, suno_prompt):
        data = self._suno_generate_lyrics(suno_prompt)
        channel = e_context["channel"]
        context = e_context["context"]
        if not data:
            error = f"response data of _suno_generate_lyrics is empty."
            raise Exception(error)
        # 获取和发送歌词
        lid = data['id']
        logger.debug(f"[Nicesuno] start to handle lyrics, lid={lid}, data={data}")
        threading.Thread(target=self._handle_lyric, args=(channel, context, lid, suno_prompt)).start()
        e_context.action = EventAction.BREAK_PASS


    # 下载和发送音乐
    def _handle_music(self, channel, context, task_id):
        # 用户信息
        actual_user_nickname = context["msg"].actual_user_nickname or context["msg"].other_user_nickname
        to_user_nickname = context["msg"].to_user_nickname

        # 初始等待时间和最大等待时间
        initial_delay_seconds = 30
        max_wait_seconds = 120
        total_waited = 0

        last_lyrics = ""
        task_data = []

        while total_waited < max_wait_seconds:
            time.sleep(initial_delay_seconds if total_waited == 0 else 10)
            total_waited += initial_delay_seconds if total_waited == 0 else 10

            # 获取任务的所有歌曲信息
            task_data = self._suno_get_music(task_id)
            logger.debug(f"[Nicesuno] Retrieved task data: {task_data}")

            if not task_data:
                raise Exception("[Nicesuno] 获取音乐信息失败！")

            # 检查每首歌的状态，如果有任何一个错误状态，则终止处理并返回错误信息
            for song in task_data:
                if song["status"] == "error":
                    error_message = song["metadata"].get("error_message", "Unknown error")
                    logger.error(f"[Nicesuno] 生成音乐失败，错误信息: {error_message}")

                    # 恢复用户的使用次数
                    user_id = self.userInfo["user_id"]
                    self.user_datas[user_id]["suno_data"]["limit"] += 1
                    write_pickle(self.user_datas_path, self.user_datas)

                    # 发送错误信息给用户
                    reply = Reply(ReplyType.TEXT, f"音乐生成失败：{error_message}。本次操作未消耗您的使用次数。")
                    channel.send(reply, context)
                    return

            # 检查是否所有歌曲的音频和封面都已生成
            all_data_ready = all(song["audio_url"] and song.get("image_large_url", "") for song in task_data)
            if all_data_ready:
                break

        # 如果在最大等待时间内没有获取到完整数据，记录警告
        if total_waited >= max_wait_seconds and not all_data_ready:
            logger.warning(f"[Nicesuno] 超时未能获取所有歌曲的音频和封面信息，部分数据可能缺失。")

        for song in task_data:
            # 解析音乐信息
            title, metadata, audio_url = song["title"], song["metadata"], song["audio_url"]
            lyrics, tags, description_prompt = metadata["prompt"], metadata["tags"], metadata['gpt_description_prompt']
            description_prompt = description_prompt if description_prompt else "自定义模式不展示"

            # 发送歌词
            if self.is_send_lyrics and lyrics != last_lyrics:
                reply_text = f"🎻{title}🎻\n\n{lyrics}\n\n🎹风格: {tags}\n👶发起人：{actual_user_nickname}\n🍀制作人：Suno\n🎤提示词: {description_prompt}"
                logger.debug(f"[Nicesuno] 发送歌词，reply_text={reply_text}")
                last_lyrics = lyrics
                reply = Reply(ReplyType.TEXT, reply_text)
                channel.send(reply, context)

            # 下载音乐
            if audio_url:
                filename = f"{int(time.time())}-{sanitize_filename(title).replace(' ', '')[:20]}"
                audio_path = os.path.join(self.music_output_dir, f"{filename}.mp3")
                logger.debug(f"[Nicesuno] 下载音乐，audio_url={audio_url}")
                self._download_file(audio_url, audio_path)

                # 发送音乐
                logger.debug(f"[Nicesuno] 发送音乐，audio_path={audio_path}")
                reply = Reply(ReplyType.FILE, audio_path)
                channel.send(reply, context)
            else:
                logger.warning(f"[Nicesuno] 音乐音频地址不存在，跳过发送音乐。")

            # 发送封面
            image_large_url = song.get("image_large_url", "")
            if self.is_send_covers and image_large_url:
                logger.debug(f"[Nicesuno] 发送封面，image_large_url={image_large_url}")
                reply = Reply(ReplyType.IMAGE_URL, image_large_url)
                channel.send(reply, context)
            else:
                logger.warning(f"[Nicesuno] 封面信息不存在或未启用发送，跳过发送封面。")


        # 初始延迟和最大等待时间
        max_wait_seconds = 60
        total_waited = 0

        video_urls = []

        while total_waited < max_wait_seconds:
            # 获取任务的所有歌曲信息
            task_data = self._suno_get_music(task_id)
            logger.debug(f"[Nicesuno] Retrieved task data: {task_data}")

            if not task_data:
                raise Exception("[Nicesuno] 获取音乐信息失败！")

            # 再次检查错误状态
            for song in task_data:
                if song["status"] == "error":
                    error_message = song["metadata"].get("error_message", "Unknown error")
                    logger.error(f"[Nicesuno] 生成音乐失败，错误信息: {error_message}")

                    # 恢复用户的使用次数
                    user_id = self.userInfo["user_id"]
                    self.user_datas[user_id]["suno_data"]["limit"] += 1
                    write_pickle(self.user_datas_path, self.user_datas)

                    # 发送错误信息给用户
                    reply = Reply(ReplyType.TEXT, f"音乐生成失败：{error_message}。本次操作未消耗您的使用次数。")
                    channel.send(reply, context)
                    return

            # 遍历所有歌曲，获取视频链接
            video_urls = [song["video_url"] for song in task_data if song["video_url"]]

            # 如果所有视频链接都存在，退出循环
            if len(video_urls) == len(task_data):
                break

            # 如果视频链接还未全部生成，等待5秒后重试
            time.sleep(5)
            total_waited += 5

        # 处理超时情况
        if len(video_urls) < len(task_data):
            logger.warning("[Nicesuno] 超时未能获取所有的视频链接。")
            reply_text = f"{to_user_nickname}，您的音乐已生成，但部分视频链接暂时无法获取，请稍后再试。"
        else:
            video_text = '\n'.join(f'视频{idx + 1}: {url}' for idx, url in enumerate(video_urls))
            reply_text = f"{to_user_nickname}已经为您创作了音乐，请查收！以下是音乐视频：\n{video_text}"

        # 发送查收提醒
        if context.get("isgroup", False):
            reply_text = f"@{actual_user_nickname}\n" + reply_text

        logger.debug(f"[Nicesuno] 发送查收提醒，reply_text={reply_text}")
        reply = Reply(ReplyType.TEXT, reply_text)
        channel.send(reply, context)
        




    # 获取和发送歌词
    def _handle_lyric(self, channel, context, lid, description_prompt=""):
        # 用户信息
        actual_user_nickname = context["msg"].actual_user_nickname or context["msg"].other_user_nickname
        # 获取歌词信息
        start_time = time.time()
        while True:
            data = self._suno_get_lyrics(lid)
            if not data:
                raise Exception("[Nicesuno] 获取歌词信息失败！")
            elif data["status"] == 'complete':
                break
            elif time.time() - start_time > 120:
                raise TimeoutError("[Nicesuno] 获取歌词信息超时！")
            time.sleep(5)
        # 发送歌词
        title, lyrics = data["title"], data["text"]
        reply_text = f"🎻{title}🎻\n\n{lyrics}\n\n👶发起人：{actual_user_nickname}\n🍀制作人：Suno\n🎤提示词: {description_prompt}"
        logger.debug(f"[Nicesuno] 发送歌词，reply_text={reply_text}")
        reply = Reply(ReplyType.TEXT, reply_text)
        channel.send(reply, context)

    # 创作音乐
    def _suno_generate_music_with_description(self, description, make_instrumental=False, retry_count=0):
        payload = {
            "gpt_description_prompt": description,
            "make_instrumental": make_instrumental,
            "mv": "chirp-v3-0",
        }
        userInfo = self.userInfo  # 使用已获取的 userInfo
        while retry_count >= 0:
            try:
                response = requests.post(f"{self.suno_api_base}/suno/submit/music", data=json.dumps(payload), headers=self.http_headers, timeout=(5, 30))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                logger.debug(f"[Nicesuno] _suno_generate_music_with_description, response={response.text}")

                logger.debug(f"[Nicesuno] UID: {userInfo['user_id']}, Type of self.user_datas[userInfo['user_id']]: {type(self.user_datas.get(userInfo['user_id']))}, Content: {self.user_datas.get(userInfo['user_id'])}")
                if self.user_datas[userInfo['user_id']]["suno_data"]["limit"] > 0:
                    self.user_datas[userInfo['user_id']]["suno_data"]["limit"] -= 1
                    write_pickle(self.user_datas_path, self.user_datas)
            
                return response.json()

            except Exception as e:
                logger.error(f"[Nicesuno] _suno_generate_music_with_description failed, description={description}, error={e}")
                retry_count -= 1
                time.sleep(5)

    # 创作音乐
    def _suno_generate_music_custom_mode(self, title=None, tags=None, lyrics=None, make_instrumental=False, retry_count=0):
        payload = {
            "title": title,
            "tags": tags,
            "prompt": lyrics,
            "make_instrumental": make_instrumental,
            "mv": "chirp-v3-0",
            "continue_clip_id": None,
            "continue_at": None,
        }
        userInfo = self.userInfo  # 使用已获取的 userInfo
        while retry_count >= 0:
            try:
                response = requests.post(f"{self.suno_api_base}/suno/submit/music", data=json.dumps(payload), headers=self.http_headers, timeout=(5, 30))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                logger.debug(f"[Nicesuno] _suno_generate_music_custom_mode, response={response.text}")

                logger.debug(f"[Nicesuno] UID: {userInfo['user_id']}, Type of self.user_datas[userInfo['user_id']]: {type(self.user_datas.get(userInfo['user_id']))}, Content: {self.user_datas.get(userInfo['user_id'])}")
                if self.user_datas[userInfo['user_id']]["suno_data"]["limit"] > 0:
                    self.user_datas[userInfo['user_id']]["suno_data"]["limit"] -= 1
                    write_pickle(self.user_datas_path, self.user_datas)
                
                return response.json()
            except Exception as e:
                logger.error(f"[Nicesuno] _suno_generate_music_custom_mode failed, title={title}, tags={tags}, lyrics={lyrics}, error={e}")
                retry_count -= 1
                time.sleep(5)

    # 获取音乐信息
    def _suno_get_music(self, aid, retry_count=6):
        while retry_count >= 0:
            try:
                logger.debug(f"[Nicesuno] Fetching music with task_id={aid}, type={type(aid)}")
                response = requests.get(f"{self.suno_api_base}/suno/fetch/{aid}", headers=self.http_headers, timeout=(5, 30))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                task_data = response.json()
                task_data = task_data['data']['data']  # 这里初始化了 task_data
                logger.debug(f"[Nicesuno] Processing {len(task_data)} songs from task_id={aid}")
                return task_data
            
            except Exception as e:
                logger.error(f"[Nicesuno] _suno_get_music failed, task_id={aid}, error={e}")
                retry_count -= 1
                time.sleep(5)
        return None

    # 创作歌词
    def _suno_generate_lyrics(self, suno_lyric_prompt, retry_count=3):
        payload = {
            "prompt": suno_lyric_prompt
        }
        while retry_count >= 0:
            try:
                response = requests.post(f"{self.suno_api_base}/generate/lyrics/", data=json.dumps(payload), timeout=(5, 30))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                logger.debug(f"[Nicesuno] _suno_generate_lyrics, response={response.text}")
                return response.json()
            except Exception as e:
                logger.error(f"[Nicesuno] _suno_generate_lyrics failed, suno_lyric_prompt={suno_lyric_prompt}, error={e}")
                retry_count -= 1
                time.sleep(5)

    # 获取歌词信息
    def _suno_get_lyrics(self, lid, retry_count=3):
        while retry_count >= 0:
            try:
                response = requests.get(f"{self.suno_api_base}/lyrics/{lid}", timeout=(5, 30))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                logger.debug(f"[Nicesuno] _suno_get_lyrics, response={response.text}")
                return response.json()
            except Exception as e:
                logger.error(f"[Nicesuno] _suno_get_lyrics failed, lid={lid}, error={e}")
                retry_count -= 1
                time.sleep(5)

    # 下载文件
    def _download_file(self, file_url, file_path, retry_count=3):
        while retry_count >= 0:
            try:
                response = requests.get(file_url, allow_redirects=True, stream=True)
                if response.status_code != 200:
                    raise Exception(f"[Nicesuno] 文件下载失败，file_url={file_url}, status_code={response.status_code}")
                with open(file_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
            except Exception as e:
                logger.error(f"[Nicesuno] 文件下载失败，file_url={file_url}, error={e}")
                retry_count -= 1
                time.sleep(5)
            else:
                break

    # 检查是否包含创作音乐的前缀
    def _check_prefix(self, content, prefix_list):
        if not prefix_list:
            return None
        for prefix in prefix_list:
            if content.startswith(prefix):
                return prefix
        return None

    # 指令处理
    def handle_command(self, e_context: EventContext):
        content = e_context['context'].content
        com = content[1:].strip().split()
        cmd = com[0]
        args = com[1:]
        if any(cmd in info["alias"] for info in COMMANDS.values()):
            cmd = next(c for c, info in COMMANDS.items() if cmd in info["alias"])
            if cmd == "suno_help":
                return Info(self.get_help_text(admin=self.userInfo.get("isadmin", False)), e_context)
            elif cmd == "suno_admin_cmd":
                if not self.userInfo["isadmin"]:
                    return Error("[suno] 您没有权限执行该操作，请先进行管理员认证", e_context)
                return Info(self.get_help_text(admin=True), e_context)
            elif cmd == "suno_admin_password":
                ok, result = self.authenticate(self.userInfo, args)
                if not ok:
                    return Error(result, e_context)
                else:
                    return Info(result, e_context)
        elif any(cmd in info["alias"] for info in ADMIN_COMMANDS.values()):
            cmd = next(c for c, info in ADMIN_COMMANDS.items() if cmd in info["alias"])
            if not self.userInfo["isadmin"]:
                return Error("[suno] 您没有权限执行该操作，请先进行管理员认证", e_context)
            # 在 handle_command 函数中添加 suno_g_info 处理逻辑
            if cmd == "suno_g_info":
                user_infos = []
                for uid, data in self.user_datas.items():
                    user_nickname = data.get("user_nickname", None)
                    limit = data.get("suno_data", {}).get("limit", "未知次数")
                    
                    if not user_nickname:  # 如果在 `user_datas` 中没有昵称
                        user_info = search_friends(uid)
                        user_nickname = user_info.get("user_nickname", None)

                    if user_nickname:  # 如果找到昵称，才添加到结果中
                        user_infos.append(f"{user_nickname}: {limit}次")

                # 将所有用户信息拼接成一个字符串
                if user_infos:
                    info_text = "当前用户昵称及剩余次数:\n" + "\n".join(user_infos)
                else:
                    info_text = "没有找到用户数据。"
                
                return Info(info_text, e_context)

            if cmd == "suno_tip":
                self.config["tip"] = not self.config["tip"]
                write_file(self.json_path, self.config)
                return Info(f"[suno] 提示功能已{'开启' if self.config['tip'] else '关闭'}", e_context)

            elif cmd == "s_limit":
                if len(args) < 1:
                    return Error("[suno] 请输入需要设置的数量", e_context)
                limit = int(args[0])
                if limit < 0:
                    return Error("[suno] 数量不能小于0", e_context)
                self.config["daily_limit"] = limit
                for index, item in self.user_datas.items():
                    if "suno_data" in item:  # 确保 suno_data 字段存在
                        self.user_datas[index]["suno_data"]["limit"] = limit
                write_pickle(self.user_datas_path, self.user_datas)
                write_file(self.json_path, self.config)
                return Info(f"[suno] 每日使用次数已设置为{limit}次", e_context)

            elif cmd == "r_limit":
                for index, item in self.user_datas.items():
                    if "suno_data" in item:  # 确保 suno_data 字段存在
                        self.user_datas[index]["suno_data"]["limit"] = self.config["daily_limit"]
                write_pickle(self.user_datas_path, self.user_datas)
                return Info(f"[suno] 所有用户每日使用次数已重置为{self.config['daily_limit']}次", e_context)

            elif cmd == "set_suno_admin_password":
                if len(args) < 1:
                    return Error("[suno] 请输入需要设置的密码", e_context)
                password = args[0]
                if self.isgroup:
                    return Error("[suno] 为避免密码泄露，请勿在群聊中进行修改", e_context)
                if len(password) < 6:
                    return Error("[suno] 密码长度不能小于6位", e_context)
                if password == self.temp_password:
                    return Error("[suno] 不能使用临时密码，请重新设置", e_context)
                if password == self.config['admin_password']:
                    return Error("[suno] 新密码不能与旧密码相同", e_context)
                self.config["admin_password"] = password
                write_file(self.json_path, self.config)
                return Info("[suno] 管理员口令设置成功", e_context)
            elif cmd == "stop_suno":
                self.issuno = False
                return Info("[suno] 服务已暂停", e_context)
            elif cmd == "enable_suno":
                self.issuno = True
                return Info("[suno] 服务已启用", e_context)
            elif cmd == "g_admin_list" and not self.isgroup:
                adminUser = self.roll["suno_admin_users"]
                t = "\n"
                nameList = t.join(f'{index+1}. {data["user_nickname"]}' for index, data in enumerate(adminUser))
                return Info(f"[suno] 管理员用户\n{nameList}", e_context)
            elif cmd == "c_admin_list" and not self.isgroup:
                self.roll["suno_admin_users"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[suno] 管理员用户已清空", e_context)
            elif cmd == "s_admin_list" and not self.isgroup:
                user_name = args[0] if args and args[0] else ""
                adminUsers = self.roll["suno_admin_users"]
                buser = self.roll["suno_busers"]
                if not args or len(args) < 1:
                    return Error("[suno] 请输入需要设置的管理员名称或ID", e_context)
                index = -1
                for i, user in enumerate(adminUsers):
                    if user["user_id"] == user_name or user["user_nickname"] == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[suno] 管理员[{adminUsers[index]['user_nickname']}]已在列表中", e_context)
                for i, user in enumerate(buser):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[suno] 用户[{user_name}]已在黑名单中，如需添加请先进行移除", e_context)
                userInfo = {
                    "user_id": user_name,
                    "user_nickname": user_name
                }
                # 判断是否是itchat平台
                if conf().get("channel_type", "wx") == "wx":
                    userInfo = search_friends(user_name)
                    # 判断user_name是否在列表中
                    if not userInfo or not userInfo["user_id"]:
                        return Error(f"[suno] 用户[{user_name}]不存在通讯录中", e_context)
                adminUsers.append(userInfo)
                self.roll["suno_admin_users"] = adminUsers
                # 写入用户列表
                write_pickle(self.roll_path, self.roll)
                return Info(f"[suno] 管理员[{userInfo['user_nickname']}]已添加到列表中", e_context)
            elif cmd == "r_admin_list" and not self.isgroup:
                text = ""
                adminUsers = self.roll["suno_admin_users"]
                if len(args) < 1:
                    return Error("[suno] 请输入需要移除的管理员名称或ID或序列号", e_context)
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(adminUsers):
                            return Error(f"[suno] 序列号[{args[0]}]不存在", e_context)
                        user_name = adminUsers[index]['user_nickname']
                        del adminUsers[index]
                        self.roll["suno_admin_users"] = adminUsers
                        write_pickle(self.roll_path, self.roll)
                        text = f"[suno] 管理员[{user_name}]已从列表中移除"
                    else:
                        user_name = args[0]
                        index = -1
                        for i, user in enumerate(adminUsers):
                            if user["user_nickname"] == user_name or user["user_id"] == user_name:
                                index = i
                                break
                        if index >= 0:
                            del adminUsers[index]
                            text = f"[suno] 管理员[{user_name}]已从列表中移除"
                            self.roll["suno_admin_users"] = adminUsers
                            write_pickle(self.roll_path, self.roll)
                        else:
                            return Error(f"[suno] 管理员[{user_name}]不在列表中", e_context)
                return Info(text, e_context)
            elif cmd == "g_wgroup" and not self.isgroup:
                text = ""
                groups = self.roll["suno_groups"]
                if len(groups) == 0:
                    text = "[suno] 白名单群组：无"
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {group}' for index, group in enumerate(groups))
                    text = f"[suno] 白名单群组\n{nameList}"
                return Info(text, e_context)
            elif cmd == "c_wgroup":
                self.roll["suno_groups"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[suno] 群组白名单已清空", e_context)
            elif cmd == "s_wgroup":
                groups = self.roll["suno_groups"]
                bgroups = self.roll["suno_bgroups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[suno] 请输入需要设置的群组名称", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    group_name = args[0]
                if group_name in groups:
                    return Error(f"[suno] 群组[{group_name}]已在白名单中", e_context)
                if group_name in bgroups:
                    return Error(f"[suno] 群组[{group_name}]已在黑名单中，如需添加请先进行移除", e_context)
                # 判断是否是itchat平台，并判断group_name是否在列表中
                if conf().get("channel_type", "wx") == "wx":
                    chatrooms = itchat.search_chatrooms(name=group_name)
                    if len(chatrooms) == 0:
                        return Error(f"[suno] 群组[{group_name}]不存在", e_context)
                groups.append(group_name)
                self.roll["suno_groups"] = groups
                write_pickle(self.roll_path, self.roll)
                return Info(f"[suno] 群组[{group_name}]已添加到白名单", e_context)
            elif cmd == "r_wgroup":
                groups = self.roll["suno_groups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[suno] 请输入需要移除的群组名称或序列号", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(groups):
                            return Error(f"[suno] 序列号[{args[0]}]不在白名单中", e_context)
                        group_name = groups[index]
                    else:
                        group_name = args[0]
                if group_name in groups:
                    groups.remove(group_name)
                    self.roll["suno_groups"] = groups
                    write_pickle(self.roll_path, self.roll)
                    return Info(f"[suno] 群组[{group_name}]已从白名单中移除", e_context)
                else:
                    return Error(f"[suno] 群组[{group_name}]不在白名单中", e_context)
            elif cmd == "g_bgroup" and not self.isgroup:
                text = ""
                bgroups = self.roll["suno_bgroups"]
                if len(bgroups) == 0:
                    text = "[suno] 黑名单群组：无"
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {group}' for index, group in enumerate(bgroups))
                    text = f"[suno] 黑名单群组\n{nameList}"
                return Info(text, e_context)
            elif cmd == "c_bgroup":
                self.roll["suno_bgroups"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[suno] 已清空黑名单群组", e_context)
            elif cmd == "s_bgroup":
                groups = self.roll["suno_groups"]
                bgroups = self.roll["suno_bgroups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[suno] 请输入需要设置的群组名称", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    group_name = args[0]
                if group_name in groups:
                    return Error(f"[suno] 群组[{group_name}]已在白名单中，如需添加请先进行移除", e_context)
                if group_name in bgroups:
                    return Error(f"[suno] 群组[{group_name}]已在黑名单中", e_context)
                # 判断是否是itchat平台，并判断group_name是否在列表中
                if conf().get("channel_type", "wx") == "wx":
                    chatrooms = itchat.search_chatrooms(name=group_name)
                    if len(chatrooms) == 0:
                        return Error(f"[suno] 群组[{group_name}]不存在", e_context)
                bgroups.append(group_name)
                self.roll["suno_bgroups"] = bgroups
                write_pickle(self.roll_path, self.roll)
                return Info(f"[suno] 群组[{group_name}]已添加到黑名单", e_context)
            elif cmd == "r_bgroup":
                bgroups = self.roll["suno_bgroups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[suno] 请输入需要移除的群组名称或序列号", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(bgroups):
                            return Error(f"[suno] 序列号[{args[0]}]不在黑名单中", e_context)
                        group_name = bgroups[index]
                    else:
                        group_name = args[0]
                if group_name in bgroups:
                    bgroups.remove(group_name)
                    self.roll["suno_bgroups"] = bgroups
                    write_pickle(self.roll_path, self.roll)
                    return Info(f"[suno] 群组[{group_name}]已从黑名单中移除", e_context)
                else:
                    return Error(f"[suno] 群组[{group_name}]不在黑名单中", e_context)
            elif cmd == "g_buser" and not self.isgroup:
                busers = self.roll["suno_busers"]
                if len(busers) == 0:
                    return Info("[suno] 黑名单用户：无", e_context)
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {data}' for index, data in enumerate(busers))
                    return Info(f"[suno] 黑名单用户\n{nameList}", e_context)
            elif cmd == "g_wuser" and not self.isgroup:
                users = self.roll["suno_users"]
                if len(users) == 0:
                    return Info("[suno] 白名单用户：无", e_context)
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {data}' for index, data in enumerate(users))
                    return Info(f"[suno] 白名单用户\n{nameList}", e_context)
            elif cmd == "c_wuser":
                self.roll["suno_users"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[suno] 用户白名单已清空", e_context)
            elif cmd == "c_buser":
                self.roll["suno_busers"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[suno] 用户黑名单已清空", e_context)
            elif cmd == "s_wuser":
                user_name = args[0] if args and args[0] else ""
                users = self.roll["suno_users"]
                busers = self.roll["suno_busers"]
                if not args or len(args) < 1:
                    return Error("[suno] 请输入需要设置的用户名称或ID", e_context)
                index = -1
                for i, user in enumerate(users):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[suno] 用户[{user_name}]已在白名单中", e_context)
                for i, user in enumerate(busers):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[suno] 用户[{user_name}]已在黑名单中，如需添加请先移除黑名单", e_context)
                # 判断是否是itchat平台
                if conf().get("channel_type", "wx") == "wx":
                    userInfo = search_friends(user_name)
                    # 判断user_name是否在列表中
                    if not userInfo or not userInfo["user_id"]:
                        return Error(f"[suno] 用户[{user_name}]不存在通讯录中", e_context)
                users.append(user_name)
                self.roll["suno_users"] = users
                write_pickle(self.roll_path, self.roll)
                return Info(f"[suno] 用户[{user_name}]已添加到白名单", e_context)
            elif cmd == "s_buser":
                user_name = args[0] if args and args[0] else ""
                users = self.roll["suno_users"]
                busers = self.roll["suno_busers"]
                if not args or len(args) < 1:
                    return Error("[suno] 请输入需要设置的用户名称或ID", e_context)
                index = -1
                for i, user in enumerate(users):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[suno] 用户[{user_name}]已在白名单中，如需添加请先移除白名单", e_context)
                for i, user in enumerate(busers):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[suno] 用户[{user_name}]已在黑名单中", e_context)
                # 判断是否是itchat平台
                if conf().get("channel_type", "wx") == "wx":
                    userInfo = search_friends(user_name)
                    # 判断user_name是否在列表中
                    if not userInfo or not userInfo["user_id"]:
                        return Error(f"[suno] 用户[{user_name}]不存在通讯录中", e_context)
                busers.append(user_name)
                self.roll["suno_busers"] = busers
                write_pickle(self.roll_path, self.roll)
                return Info(f"[suno] 用户[{user_name}]已添加到黑名单", e_context)
            elif cmd == "r_wuser":
                text = ""
                users = self.roll["suno_users"]
                if len(args) < 1:
                    return Error("[suno] 请输入需要移除的用户名称或ID或序列号", e_context)
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(users):
                            return Error(f"[suno] 序列号[{args[0]}]不存在", e_context)
                        user_name = users[index]
                        del users[index]
                        self.roll["suno_users"] = users
                        write_pickle(self.roll_path, self.roll)
                        text = f"[suno] 用户[{user_name}]已从白名单中移除"
                    else:
                        user_name = args[0]
                        index = -1
                        for i, user in enumerate(users):
                            if user == user_name:
                                index = i
                                break
                        if index >= 0:
                            del users[index]
                            text = f"[suno] 用户[{user_name}]已从白名单中移除"
                            self.roll["suno_users"] = users
                            write_pickle(self.roll_path, self.roll)
                        else:
                            return Error(f"[suno] 用户[{user_name}]不在白名单中", e_context)
                return Info(text, e_context)
            elif cmd == "r_buser":
                text = ""
                busers = self.roll["suno_busers"]
                if len(args) < 1:
                    return Error("[suno] 请输入需要移除的用户名称或ID或序列号", e_context)
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(busers):
                            return Error(f"[suno] 序列号[{args[0]}]不存在", e_context)
                        user_name = busers[index]
                        del busers[index]
                        self.roll["suno_busers"] = busers
                        write_pickle(self.roll_path, self.roll)
                        text = f"[suno] 用户[{user_name}]已从黑名单中移除"
                    else:
                        user_name = args[0]
                        index = -1
                        for i, user in enumerate(busers):
                            if user == user_name:
                                index = i
                                break
                        if index >= 0:
                            del busers[index]
                            text = f"[suno] 用户[{user_name}]已从黑名单中移除"
                            self.roll["suno_busers"] = busers
                            write_pickle(self.roll_path, self.roll)
                        else:
                            return Error(f"[suno] 用户[{user_name}]不在黑名单中", e_context)
                return Info(text, e_context)
            else:
                return "Bye"
                
    def authenticate(self, userInfo, args) -> Tuple[bool, str]:
        isgroup = userInfo["isgroup"]
        isadmin = userInfo["isadmin"]
        if isgroup:
            return False, "[suno] 为避免密码泄露，请勿在群聊中认证"

        if isadmin:
            return False, "[suno] 管理员账号无需认证"

        if len(args) != 1:
            return False, "[suno] 请输入密码"

        password = args[0]
        if password == self.config['suno_admin_password'] or password == self.temp_password:
            self.roll["suno_admin_users"].append({
                "user_id": userInfo["user_id"],
                "user_nickname": userInfo["user_nickname"]
            })
            write_pickle(self.roll_path, self.roll)
            return True, f"[suno] 认证成功 {'，请尽快设置口令' if password == self.temp_password else ''}"
        else:
            return False, "[suno] 认证失败"

    
    def get_user_info(self, e_context: EventContext):
            # 获取当前时间戳
            current_timestamp = time.time()
            # 将当前时间戳和给定时间戳转换为日期字符串
            current_date = time.strftime("%Y-%m-%d", time.localtime(current_timestamp))
            groups = self.roll["suno_groups"]
            bgroups = self.roll["suno_bgroups"]
            users = self.roll["suno_users"]
            logger.debug(f"[Nicesuno] Type of users: {type(users)}, Content: {users}")
            busers = self.roll["suno_busers"]
            suno_admin_users = self.roll["suno_admin_users"]
            context = e_context['context']
            msg: ChatMessage = context["msg"]
            isgroup = context.get("isgroup", False)
            # 写入用户信息，企业微信没有from_user_nickname，所以使用from_user_id代替
            uid = msg.from_user_id if not isgroup else msg.actual_user_id
            uname = (msg.from_user_nickname if msg.from_user_nickname else uid) if not isgroup else msg.actual_user_nickname
            logger.debug(f"[Nicesuno] UID: {uid}, User data keys: {list(self.user_datas.keys())}")
            if uid not in self.user_datas:
                logger.warning(f"[Nicesuno] UID: {uid} not found in user_datas")
            else:
                logger.debug(f"[Nicesuno] Found UID: {uid}, Data: {self.user_datas[uid]}")

            userInfo = {
                "user_id": uid,
                "user_nickname": uname,
                "isgroup": isgroup,
                "group_id": msg.from_user_id if isgroup else "",
                "group_name": msg.from_user_nickname if isgroup else "",
            }
            # 判断是否是新的一天
            logger.debug(f"[Nicesuno] UID: {uid}, Type of self.user_datas[uid]: {type(self.user_datas.get(uid))}, Content: {self.user_datas.get(uid)}")
            if uid not in self.user_datas or "suno_data" not in self.user_datas[uid] or "suno_data" not in self.user_datas[uid] or self.user_datas[uid]["suno_data"]["time"] != current_date:
                suno_data = {
                    "limit": self.config["daily_limit"],
                    "time": current_date
                }
                if uid in self.user_datas and self.user_datas[uid]["suno_data"]:
                    self.user_datas[uid]["suno_data"] = suno_data
                else:
                    self.user_datas[uid] = {
                        "suno_data": suno_data
                    }
                write_pickle(self.user_datas_path, self.user_datas)
            limit = self.user_datas[uid]["suno_data"]["limit"] if "suno_data" in self.user_datas[uid] and "limit" in self.user_datas[uid]["suno_data"] and self.user_datas[uid]["suno_data"]["limit"] and self.user_datas[uid]["suno_data"]["limit"] > 0 else False
            userInfo['limit'] = limit
            userInfo['isadmin'] = uid in [user["user_id"] for user in suno_admin_users]

            # 判断白名单用户
            if isinstance(users, list):
                if all(isinstance(user, dict) for user in users):
                    userInfo['iswuser'] = uname in [user["user_nickname"] for user in users]
                else:
                    userInfo['iswuser'] = uname in users  # users 中为字符串时
            else:
                userInfo['iswuser'] = False
            
            # 判断黑名单用户
            if isinstance(busers, list):
                if all(isinstance(user, dict) for user in busers):
                    userInfo['isbuser'] = uname in [user["user_nickname"] for user in busers]
                else:
                    userInfo['isbuser'] = uname in busers  # busers 中为字符串时
            else:
                userInfo['isbuser'] = False
            
            #userInfo['iswuser'] = uname in [user["user_nickname"] for user in users]
            #userInfo['isbuser'] = uname in [user["user_nickname"] for user in busers]
            userInfo['iswgroup'] = userInfo["group_name"] in groups
            userInfo['isbgroup'] = userInfo["group_name"] in bgroups
            return userInfo
    
  
