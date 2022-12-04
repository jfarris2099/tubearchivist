"""
Functionality:
- Download comments
- Index comments in ES
- Retrieve comments from ES
"""

from datetime import datetime

from home.src.download.yt_dlp_base import YtWrap
from home.src.es.connect import ElasticWrap
from home.src.ta.config import AppConfig
from home.src.ta.ta_redis import RedisArchivist


class Comments:
    """hold all comments functionality"""

    def __init__(self, youtube_id, config=False):
        self.youtube_id = youtube_id
        self.es_path = f"ta_comment/_doc/{youtube_id}"
        self.json_data = False
        self.config = config
        self.is_activated = False
        self.comments_format = False

    def build_json(self, notify=False):
        """build json document for es"""
        print(f"{self.youtube_id}: get comments")
        self.check_config()
        if not self.is_activated:
            return

        self._send_notification(notify)
        comments_raw, channel_id = self.get_yt_comments()
        if comments_raw:
            self.format_comments(comments_raw)
        else:
            self.comments_format = []

        self.json_data = {
            "youtube_id": self.youtube_id,
            "comment_last_refresh": int(datetime.now().strftime("%s")),
            "comment_channel_id": channel_id,
            "comment_comments": self.comments_format,
        }

    def check_config(self):
        """read config if not attached"""
        if not self.config:
            self.config = AppConfig().config

        self.is_activated = bool(self.config["downloads"]["comment_max"])

    @staticmethod
    def _send_notification(notify):
        """send notification for download post process message"""
        if not notify:
            return

        key = "message:download"
        idx, total_videos = notify
        message = {
            "status": key,
            "level": "info",
            "title": "Download and index comments",
            "message": f"Progress: {idx + 1}/{total_videos}",
        }

        RedisArchivist().set_message(key, message)

    def build_yt_obs(self):
        """
        get extractor config
        max-comments,max-parents,max-replies,max-replies-per-thread
        """
        max_comments = self.config["downloads"]["comment_max"]
        max_comments_list = [i.strip() for i in max_comments.split(",")]
        comment_sort = self.config["downloads"]["comment_sort"]

        yt_obs = {
            "skip_download": True,
            "quiet": False,
            "getcomments": True,
            "extractor_args": {
                "youtube": {
                    "max_comments": max_comments_list,
                    "comment_sort": [comment_sort],
                }
            },
        }

        return yt_obs

    def get_yt_comments(self):
        """get comments from youtube"""
        yt_obs = self.build_yt_obs()
        info_json = YtWrap(yt_obs).extract(self.youtube_id)
        comments_raw = info_json.get("comments")
        channel_id = info_json.get("channel_id")
        return comments_raw, channel_id

    def format_comments(self, comments_raw):
        """process comments to match format"""
        comments = []

        for comment in comments_raw:
            cleaned_comment = self.clean_comment(comment)
            comments.append(cleaned_comment)

        self.comments_format = comments

    def clean_comment(self, comment):
        """parse metadata from comment for indexing"""
        time_text_datetime = datetime.utcfromtimestamp(comment["timestamp"])

        if time_text_datetime.hour == 0 and time_text_datetime.minute == 0:
            format_string = "%Y-%m-%d"
        else:
            format_string = "%Y-%m-%d %H:%M"

        time_text = time_text_datetime.strftime(format_string)

        cleaned_comment = {
            "comment_id": comment["id"],
            "comment_text": comment["text"].replace("\xa0", ""),
            "comment_timestamp": comment["timestamp"],
            "comment_time_text": time_text,
            "comment_likecount": comment["like_count"],
            "comment_is_favorited": comment["is_favorited"],
            "comment_author": comment["author"],
            "comment_author_id": comment["author_id"],
            "comment_author_thumbnail": comment["author_thumbnail"],
            "comment_author_is_uploader": comment["author_is_uploader"],
            "comment_parent": comment["parent"],
        }

        return cleaned_comment

    def upload_comments(self):
        """upload comments to es"""
        if not self.is_activated:
            return

        _, _ = ElasticWrap(self.es_path).put(self.json_data)

        vid_path = f"ta_video/_update/{self.youtube_id}"
        data = {"doc": {"comment_count": len(self.comments_format)}}
        _, _ = ElasticWrap(vid_path).post(data=data)

    def delete_comments(self):
        """delete comments from es"""
        print(f"{self.youtube_id}: delete comments")
        _, _ = ElasticWrap(self.es_path).delete(refresh=True)

    def get_es_comments(self):
        """get comments from ES"""
        response, statuscode = ElasticWrap(self.es_path).get()
        if statuscode == 404:
            print(f"comments: not found {self.youtube_id}")
            return False

        return response.get("_source")

    def reindex_comments(self):
        """update comments from youtube"""
        self.check_config()
        if not self.is_activated:
            return

        self.build_json()
        es_comments = self.get_es_comments()

        if not self.comments_format:
            return

        if not self.comments_format and es_comments["comment_comments"]:
            # don't overwrite comments in es
            return

        self.delete_comments()
        self.upload_comments()
