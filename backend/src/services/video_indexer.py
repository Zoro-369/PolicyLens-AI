import os
import time
import logging
import requests
import yt_dlp  
from azure.identity import DefaultAzureCredential

from backend.src.config import get_settings,require

logger = logging.getLogger("policy_lens_ai.video_indexer")
_TOKEN_REFRESH_AFTER_S = 20*60  # refresh token every 20 minutes

class VideoIndexerTimeout(TimeoutError):
    """Video Indexer did not finish processing within the allowed time."""

class VideoIndexerService:
    def __init__(self):
        self.cfg = get_settings()
        require(self.cfg, ["vi_account_id", "vi_location", "vi_name",
                           "subscription_id", "resource_group"])
        self.account_id = self.cfg.vi_account_id
        self.location = self.cfg.vi_location
        self.subscription_id = self.cfg.subscription_id
        self.resource_group = self.cfg.resource_group
        self.vi_name = self.cfg.vi_name
        self.credential = DefaultAzureCredential()

    def get_access_token(self):
        """Generates an ARM Access Token."""
        try:
            token_object = self.credential.get_token("https://management.azure.com/.default")
            return token_object.token
        except Exception as e:
            logger.error(f"Failed to get Azure Token: {e}")
            raise

    def get_account_token(self, arm_access_token):
        """Exchanges ARM token for Video Indexer Account Token."""
        url = (
            f"https://management.azure.com/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.VideoIndexer/accounts/{self.vi_name}"
            f"/generateAccessToken?api-version=2024-01-01"
        )
        headers = {"Authorization": f"Bearer {arm_access_token}"}
        payload = {"permissionType": "Contributor", "scope": "Account"}
        response = requests.post(url, headers=headers, json=payload,timeout=self.cfg.request_timeout_s)
        if response.status_code != 200:
            raise Exception(f"Failed to get VI Account Token: {response.text}")
        return response.json().get("accessToken")
    
    def _fresh_account_token(self):
        return self.get_account_token(self.get_access_token())

    # --- NEW FUNCTION: Download from YouTube ---
    def download_youtube_video(self, url, output_path="temp_video.mp4"):
        """Downloads a YouTube video to a local file."""
        logger.info(f"Downloading YouTube video: {url}")
        
        ydl_opts = {
         'format': 'best',
         'outtmpl': output_path, # output template
         'quiet': False,
         'no_warnings': False,
    # Add these options:
         'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
         'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

}
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            logger.info("Download complete.")
            return output_path
        except Exception as e:
            raise Exception(f"YouTube Download Failed: {str(e)}")

    # --- UPDATED FUNCTION: Upload Local File ---
    def upload_video(self, video_path, video_name):
        """Uploads a LOCAL FILE to Azure Video Indexer."""
        vi_token = self._fresh_account_token()

        api_url = f"https://api.videoindexer.ai/{self.location}/Accounts/{self.account_id}/Videos"
        
        params = {
            "accessToken": vi_token,
            "name": video_name,
            "privacy": "Private",
            "indexingPreset": "Default",
            # We removed "videoUrl" because we are sending a file payload instead
        }
        
        logger.info(f"Uploading file {video_path} to Azure...")
        # timeout=(self.cfg.request_timeout_s, self.cfg.upload_timeout_s)
        # Open the file in binary mode and stream it to Azure
        with open(video_path, 'rb') as video_file:
            files = {'file': video_file}
            response = requests.post(api_url, params=params, files=files,timeout=(self.cfg.request_timeout_s, self.cfg.upload_timeout_s),)
        
        if response.status_code != 200:
            raise Exception(f"Azure Upload Failed: {response.text}")
            
        return response.json().get("id")

    def wait_for_processing(self, video_id, max_wait_s=None,
                            initial_interval_s=None, max_interval_s=None):
        """Poll until the video is processed, with exponential backoff and a hard deadline.

        Raises:
            VideoIndexerTimeout: if processing exceeds max_wait_s.
            Exception: if Azure reports Failed or Quarantined.
        """
        max_wait_s = self.cfg.vi_max_wait_s if max_wait_s is None else max_wait_s
        interval = self.cfg.vi_poll_initial_s if initial_interval_s is None else initial_interval_s
        max_interval = self.cfg.vi_poll_max_interval_s if max_interval_s is None else max_interval_s

        logger.info(f"Waiting for video {video_id} to process (max {max_wait_s:.0f}s)...")
        deadline = time.monotonic() + max_wait_s
        token, token_time = None, 0.0

        while True:
            if token is None or time.monotonic() - token_time > _TOKEN_REFRESH_AFTER_S:
                token, token_time = self._fresh_account_token(), time.monotonic()

            url = f"https://api.videoindexer.ai/{self.location}/Accounts/{self.account_id}/Videos/{video_id}/Index"
            state = None
            try:
                response = requests.get(url, params={"accessToken": token},
                                        timeout=self.cfg.request_timeout_s)
                if response.status_code in (429,) or response.status_code >= 500:
                    logger.warning(f"Transient VI status {response.status_code}; will retry.")
                elif response.status_code != 200:
                    raise Exception(f"Video Indexer status check failed ({response.status_code}): {response.text}")
                else:
                    data = response.json()
                    state = data.get("state")
                    if state == "Processed":
                        return data
                    if state == "Failed":
                        raise Exception("Video Indexing Failed in Azure.")
                    if state == "Quarantined":
                        raise Exception("Video Quarantined (Copyright/Content Policy Violation).")
            except requests.RequestException as e:
                logger.warning(f"Network error while polling VI: {e}; will retry.")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VideoIndexerTimeout(
                    f"Video Indexer did not finish within {max_wait_s:.0f}s (last state: {state})."
                )
            wait = min(interval, remaining)
            logger.info(f"Status: {state}... waiting {wait:.0f}s")
            time.sleep(wait)
            interval = min(interval * 1.5, max_interval)

    def extract_data(self, vi_json):
        """Parses the JSON into our State format."""
        transcript_lines = []
        for v in vi_json.get("videos", []):
            for insight in v.get("insights", {}).get("transcript", []):
                transcript_lines.append(insight.get("text"))

        ocr_lines = []
        for v in vi_json.get("videos", []):
            for insight in v.get("insights", {}).get("ocr", []):
                ocr_lines.append(insight.get("text"))

        return {
            "transcript": " ".join(t for t in transcript_lines if t),
            "ocr_text": [o for o in ocr_lines if o],
            "video_metadata": {
                "duration": vi_json.get("summarizedInsights", {}).get("duration", {}).get("seconds"),
                "platform": "youtube"
            }
        }