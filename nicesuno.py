# encoding:utf-8
import os
import re
import json
import time
import requests
import threading
from typing import List
from pathvalidate import sanitize_filename

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *

@plugins.register(
    name="Nicesuno",
    desire_priority=90,
    hidden=False,
    desc="一款基于Suno和Suno-API创作音乐的插件。",
    version="1.3",
    author="空心菜",
)
class Nicesuno(Plugin):
    def __init__(self):
        super().__init__()
        try:
            # 加载配置
            conf = super().load_config()
            # 配置不存在则使用默认配置
            if not conf:
                logger.debug("[Nicesuno] config.json not found, config.json.template used.")
                curdir = os.path.dirname(__file__)
                config_path = os.path.join(curdir, "config.json.template")
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        conf = json.load(f)
            self.suno_api_bases = conf.get("suno_api_bases", [])
            self.http_headers = {
                'Authorization': f'Bearer {conf.get("suno_api_token", "")}',
                'Content-Type': 'application/json'
            }
            self.music_create_prefixes = conf.get("music_create_prefixes", [])
            self.instrumental_create_prefixes = conf.get("instrumental_create_prefixes", [])
            self.lyrics_create_prefixes = conf.get("lyrics_create_prefixes", [])
            self.music_output_dir = conf.get("music_output_dir", "/tmp")
            self.is_send_lyrics = conf.get("is_send_lyrics", True)
            self.is_send_covers = conf.get("is_send_covers", True)
            if not os.path.exists(self.music_output_dir):
                logger.info(f"[Nicesuno] music_output_dir={self.music_output_dir} not exists, create it.")
                os.makedirs(self.music_output_dir)
            if self.suno_api_bases and isinstance(self.suno_api_bases, List) \
                    and self.music_create_prefixes and isinstance(self.music_create_prefixes, List):
                self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
                logger.info("[Nicesuno] inited")
            else:
                logger.warn("[Nicesuno] init failed because suno_api_bases or music_create_prefixes is incorrect.")
            # 待实现：部署多套Suno-API，实现限额后自动切换Suno账号
            self.suno_api_base = self.suno_api_bases[0]
        except Exception as e:
            logger.error(f"[Nicesuno] init failed, ignored.")
            raise e

    def on_handle_context(self, e_context: EventContext):
        try:
            # 判断是否是TEXT类型消息
            context = e_context["context"]
            if context.type != ContextType.TEXT:
                return
            content = context.content
            logger.debug(f"[Nicesuno] on_handle_context. content={content}")

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
            reply = Reply(ReplyType.TEXT, "抱歉！创作失败了，请稍后再试🥺")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    # 创作音乐
    def _create_music(self, e_context, suno_prompt, make_instrumental=False):
        custom_mode = False
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
            threading.Thread(target=self._handle_music, args=(channel, context, aids)).start()
            reply = Reply(ReplyType.TEXT, f"{to_user_nickname}正在为您创作音乐，请稍等☕")

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
    def _handle_music(self, channel, context, aids: List):
        # 用户信息
        actual_user_nickname = context["msg"].actual_user_nickname or context["msg"].other_user_nickname
        to_user_nickname = context["msg"].to_user_nickname
        # 获取歌词和音乐
        initial_delay_seconds = 15
        last_lyrics = ""
        for aid in aids:
            # 获取音乐信息
            start_time = time.time()
            while True:
                if initial_delay_seconds:
                    time.sleep(initial_delay_seconds)
                    initial_delay_seconds = 0
                data = self._suno_get_music(aid)
                if not data:
                    raise Exception("[Nicesuno] 获取音乐信息失败！")
                elif data["audio_url"]:
                    break
                elif time.time() - start_time > 180:
                    raise TimeoutError("[Nicesuno] 获取音乐信息超时！")
                time.sleep(5)
            # 解析音乐信息
            title, metadata, audio_url = data["title"], data["metadata"], data["audio_url"]
            lyrics, tags, description_prompt = metadata["prompt"], metadata["tags"], metadata['gpt_description_prompt']
            description_prompt = description_prompt if description_prompt else "自定义模式不展示"
            # 发送歌词
            if not self.is_send_lyrics:
                logger.debug(f"[Nicesuno] 发送歌词开关关闭，不发送歌词！")
            elif lyrics == last_lyrics:
                logger.debug("[Nicesuno] 歌词和上次相同，不再重复发送歌词！")
            else:
                reply_text = f"🎻{title}🎻\n\n{lyrics}\n\n🎹风格: {tags}\n👶发起人：{actual_user_nickname}\n🍀制作人：Suno\n🎤提示词: {description_prompt}"
                logger.debug(f"[Nicesuno] 发送歌词，reply_text={reply_text}")
                last_lyrics = lyrics
                reply = Reply(ReplyType.TEXT, reply_text)
                channel.send(reply, context)
            # 下载音乐
            filename = f"{int(time.time())}-{sanitize_filename(title).replace(' ', '')[:20]}"
            audio_path = os.path.join(self.music_output_dir, f"{filename}.mp3")
            logger.debug(f"[Nicesuno] 下载音乐，audio_url={audio_url}")
            self._download_file(audio_url, audio_path)
            # 发送音乐
            logger.debug(f"[Nicesuno] 发送音乐，audio_path={audio_path}")
            reply = Reply(ReplyType.FILE, audio_path)
            channel.send(reply, context)
            # 发送封面
            if not self.is_send_covers:
                logger.debug(f"[Nicesuno] 发送封面开关关闭，不发送封面！")
            else:
                # 获取封面信息
                start_time = time.time()
                while True:
                    data = self._suno_get_music(aid)
                    if not data:
                        #raise Exception("[Nicesuno] 获取封面信息失败！")
                        logger.warning("[Nicesuno] 获取封面信息失败！")
                        break
                    elif data["image_url"]:
                        break
                    elif time.time() - start_time > 60:
                        #raise TimeoutError("[Nicesuno] 获取封面信息超时！")
                        logger.warning("[Nicesuno] 获取封面信息超时！")
                        break
                    time.sleep(5)
                if data and data["image_url"]:
                    image_url = data["image_url"]
                    logger.debug(f"[Nicesuno] 发送封面，image_url={image_url}")
                    reply = Reply(ReplyType.IMAGE_URL, image_url)
                    channel.send(reply, context)
                else:
                    logger.warning(f"[Nicesuno] 获取封面信息失败，放弃发送封面！")
        # 获取视频地址
        video_urls = []
        for aid in aids:
            # 获取视频地址
            start_time = time.time()
            while True:
                data = self._suno_get_music(aid)
                if not data:
                    #raise Exception("[Nicesuno] 获取视频地址失败！")
                    logger.warning("[Nicesuno] 获取视频地址失败！")
                    video_urls.append("获取失败！")
                    break
                elif data["video_url"]:
                    video_urls.append(data["video_url"])
                    break
                elif time.time() - start_time > 180:
                    #raise TimeoutError("[Nicesuno] 获取视频地址超时！")
                    logger.warning("[Nicesuno] 获取视频地址超时！")
                    video_urls.append("获取超时！")
                time.sleep(10)
        # 查收提醒
        video_text = '\n'.join(f'视频{idx+1}: {url}' for idx, url in zip(range(len(video_urls)), video_urls))
        reply_text = f"{to_user_nickname}已经为您创作了音乐，请查收！以下是音乐视频：\n{video_text}"
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
        while retry_count >= 0:
            try:
                response = requests.post(f"{self.suno_api_base}/suno/submit/music", data=json.dumps(payload), headers=self.http_headers, timeout=(5, 30))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                logger.debug(f"[Nicesuno] _suno_generate_music_with_description, response={response.text}")
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
        while retry_count >= 0:
            try:
                response = requests.post(f"{self.suno_api_base}/suno/submit/music", data=json.dumps(payload), headers=self.http_headers, timeout=(5, 30))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                logger.debug(f"[Nicesuno] _suno_generate_music_custom_mode, response={response.text}")
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
                response = requests.get(f"{self.suno_api_base}/suno/fetch/{aid}", headers=self.http_headers, timeout=(5, 180))
                if response.status_code != 200:
                    raise Exception(f"status_code is not ok, status_code={response.status_code}")
                logger.debug(f"[Nicesuno] _suno_get_music, response={response.text}")
                return response.json()['data']['data'][0]
            except Exception as e:
                logger.error(f"[Nicesuno] _suno_get_music failed, task_id={aid}, error={e}")
                retry_count -= 1
                time.sleep(5)

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

    # 帮助文档
    def get_help_text(self, **kwargs):
        return "使用Suno创作音乐。\n1.创作声乐\n用法：唱/演唱<提示词>\n示例：唱明天会更好。\n\n2.创作器乐\n用法：演奏<提示词>\n示例：演奏明天会更好。\n\n3.创作歌词\n用法：写歌/作词<提示词>\n示例：写歌明天会更好。\n\n4.自定义模式\n用法：\n唱/演唱/演奏\n标题: <标题>\n风格: <风格1> <风格2> ...\n<歌词>\n备注：前三行必须为创作前缀、标题、风格，<标题><风格><歌词>三个值可以为空，但<风格><歌词>不可同时为空！"
