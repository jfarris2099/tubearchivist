"""
Functionality:
- handle download queue
- linked with ta_dowload index
"""

import json
from datetime import datetime

from home.src.download.subscriptions import (
    ChannelSubscription,
    PlaylistSubscription,
)
from home.src.download.thumbnails import ThumbManager
from home.src.download.yt_dlp_base import YtWrap
from home.src.es.connect import ElasticWrap, IndexPaginate
from home.src.index.playlist import YoutubePlaylist
from home.src.ta.config import AppConfig
from home.src.ta.helper import DurationConverter
from home.src.ta.ta_redis import RedisArchivist


class PendingIndex:
    """base class holding all export methods"""

    def __init__(self):
        self.all_pending = False
        self.all_ignored = False
        self.all_videos = False
        self.all_channels = False
        self.channel_overwrites = False
        self.video_overwrites = False
        self.to_skip = False

    def get_download(self):
        """get a list of all pending videos in ta_download"""
        data = {
            "query": {"match_all": {}},
            "sort": [{"timestamp": {"order": "asc"}}],
        }
        all_results = IndexPaginate("ta_download", data).get_results()

        self.all_pending = []
        self.all_ignored = []
        self.to_skip = []

        for result in all_results:
            self.to_skip.append(result["youtube_id"])
            if result["status"] == "pending":
                self.all_pending.append(result)
            elif result["status"] == "ignore":
                self.all_ignored.append(result)

    def get_indexed(self):
        """get a list of all videos indexed"""
        data = {
            "query": {"match_all": {}},
            "sort": [{"published": {"order": "desc"}}],
        }
        self.all_videos = IndexPaginate("ta_video", data).get_results()
        for video in self.all_videos:
            self.to_skip.append(video["youtube_id"])

    def get_channels(self):
        """get a list of all channels indexed"""
        self.all_channels = []
        self.channel_overwrites = {}
        data = {
            "query": {"match_all": {}},
            "sort": [{"channel_id": {"order": "asc"}}],
        }
        channels = IndexPaginate("ta_channel", data).get_results()

        for channel in channels:
            channel_id = channel["channel_id"]
            self.all_channels.append(channel_id)
            if channel.get("channel_overwrites"):
                self.channel_overwrites.update(
                    {channel_id: channel.get("channel_overwrites")}
                )

        self._map_overwrites()

    def _map_overwrites(self):
        """map video ids to channel ids overwrites"""
        self.video_overwrites = {}
        for video in self.all_pending:
            video_id = video["youtube_id"]
            channel_id = video["channel_id"]
            overwrites = self.channel_overwrites.get(channel_id, False)
            if overwrites:
                self.video_overwrites.update({video_id: overwrites})


class PendingInteract:
    """interact with items in download queue"""

    def __init__(self, video_id=False, status=False):
        self.video_id = video_id
        self.status = status

    def delete_item(self):
        """delete single item from pending"""
        path = f"ta_download/_doc/{self.video_id}"
        _, _ = ElasticWrap(path).delete(refresh=True)

    def delete_by_status(self):
        """delete all matching item by status"""
        data = {"query": {"term": {"status": {"value": self.status}}}}
        path = "ta_download/_delete_by_query"
        _, _ = ElasticWrap(path).post(data=data)

    def update_status(self):
        """update status field of pending item"""
        data = {"doc": {"status": self.status}}
        path = f"ta_download/_update/{self.video_id}"
        _, _ = ElasticWrap(path).post(data=data)


class PendingList(PendingIndex):
    """manage the pending videos list"""

    yt_obs = {
        "default_search": "ytsearch",
        "quiet": True,
        "check_formats": "selected",
        "noplaylist": True,
        "writethumbnail": True,
        "simulate": True,
        "socket_timeout": 3,
    }

    def __init__(self, youtube_ids=False):
        super().__init__()
        self.config = AppConfig().config
        self.youtube_ids = youtube_ids
        self.to_skip = False
        self.missing_videos = False

    def parse_url_list(self):
        """extract youtube ids from list"""
        self.missing_videos = []
        self.get_download()
        self.get_indexed()
        for entry in self.youtube_ids:
            # notify
            mess_dict = {
                "status": "message:add",
                "level": "info",
                "title": "Adding to download queue.",
                "message": "Extracting lists",
            }
            RedisArchivist().set_message("message:add", mess_dict, expire=True)
            self._process_entry(entry)

    def _process_entry(self, entry):
        """process single entry from url list"""
        if entry["type"] == "video":
            self._add_video(entry["url"])
        elif entry["type"] == "channel":
            self._parse_channel(entry["url"])
        elif entry["type"] == "playlist":
            self._parse_playlist(entry["url"])
            PlaylistSubscription().process_url_str([entry], subscribed=False)
        else:
            raise ValueError(f"invalid url_type: {entry}")

    def _add_video(self, url):
        """add video to list"""
        if url not in self.missing_videos and url not in self.to_skip:
            self.missing_videos.append(url)
        else:
            print(f"{url}: skipped adding already indexed video to download.")

    def _parse_channel(self, url):
        """add all videos of channel to list"""
        video_results = ChannelSubscription().get_last_youtube_videos(
            url, limit=False
        )
        youtube_ids = [i[0] for i in video_results]
        for video_id in youtube_ids:
            self._add_video(video_id)

    def _parse_playlist(self, url):
        """add all videos of playlist to list"""
        playlist = YoutubePlaylist(url)
        playlist.build_json()
        video_results = playlist.json_data.get("playlist_entries")
        youtube_ids = [i["youtube_id"] for i in video_results]
        for video_id in youtube_ids:
            self._add_video(video_id)

    def add_to_pending(self, status="pending"):
        """add missing videos to pending list"""
        self.get_channels()
        bulk_list = []

        for idx, youtube_id in enumerate(self.missing_videos):
            print(f"{youtube_id}: add to download queue")
            video_details = self.get_youtube_details(youtube_id)
            if not video_details:
                continue

            video_details["status"] = status
            action = {"create": {"_id": youtube_id, "_index": "ta_download"}}
            bulk_list.append(json.dumps(action))
            bulk_list.append(json.dumps(video_details))

            url = video_details["vid_thumb_url"]
            ThumbManager(youtube_id).download_video_thumb(url)

            self._notify_add(idx)

        if bulk_list:
            # add last newline
            bulk_list.append("\n")
            query_str = "\n".join(bulk_list)
            _, _ = ElasticWrap("_bulk").post(query_str, ndjson=True)

    def _notify_add(self, idx):
        """send notification for adding videos to download queue"""
        progress = f"{idx + 1}/{len(self.missing_videos)}"
        mess_dict = {
            "status": "message:add",
            "level": "info",
            "title": "Adding new videos to download queue.",
            "message": "Progress: " + progress,
        }
        if idx + 1 == len(self.missing_videos):
            expire = 4
        else:
            expire = True

        RedisArchivist().set_message("message:add", mess_dict, expire=expire)
        if idx + 1 % 25 == 0:
            print("adding to queue progress: " + progress)

    def get_youtube_details(self, youtube_id):
        """get details from youtubedl for single pending video"""
        vid = YtWrap(self.yt_obs, self.config).extract(youtube_id)
        if not vid:
            return False

        if vid.get("id") != youtube_id:
            # skip premium videos with different id
            print(f"{youtube_id}: skipping premium video, id not matching")
            return False
        # stop if video is streaming live now
        if vid["live_status"] in ["is_upcoming", "is_live"]:
            return False

        return self._parse_youtube_details(vid)

    def _parse_youtube_details(self, vid):
        """parse response"""
        vid_id = vid.get("id")
        duration_str = DurationConverter.get_str(vid["duration"])
        if duration_str == "NA":
            print(f"skip extracting duration for: {vid_id}")
        published = datetime.strptime(vid["upload_date"], "%Y%m%d").strftime(
            "%Y-%m-%d"
        )

        # build dict
        youtube_details = {
            "youtube_id": vid_id,
            "channel_name": vid["channel"],
            "vid_thumb_url": vid["thumbnail"],
            "title": vid["title"],
            "channel_id": vid["channel_id"],
            "duration": duration_str,
            "published": published,
            "timestamp": int(datetime.now().timestamp()),
        }
        if self.all_channels:
            youtube_details.update(
                {"channel_indexed": vid["channel_id"] in self.all_channels}
            )
        return youtube_details
