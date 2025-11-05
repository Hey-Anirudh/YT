import os
import asyncio
from fastapi import FastAPI, HTTPException, Request, Depends, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
import uvicorn
import aiohttp
from typing import Optional, Dict, List
import time
import hashlib
from pathlib import Path
import re
import logging
import subprocess
import tempfile
import shutil
from utils.downloader import download

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
API_KEY = os.getenv("API_KEY", "Shadow")
DOWNLOAD_FOLDER = "downloads"
CACHE_TTL = 3600  # 1 hour cache
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7628021522:AAHTOaNUwriDHV9XqIsYj7fTd-AbKh-Tkec")
TELEGRAM_DB_CHANNEL = os.getenv("TELEGRAM_DB_CHANNEL", "@shizuku_db")
TELEGRAM_UPLOAD_CHANNEL = os.getenv("TELEGRAM_UPLOAD_CHANNEL", "@shizuku_db")

# File size limits for Telegram (50MB for documents, 2GB for premium)
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # 50MB
TELEGRAM_VIDEO_LIMIT = 50 * 1024 * 1024  # 50MB for videos

# Ensure downloads directory exists
Path(DOWNLOAD_FOLDER).mkdir(exist_ok=True)

# Cache for download status
download_cache = {}
telegram_db_cache = {}
# Track background upload tasks
background_upload_tasks = {}

class MediaCompressor:
    """Handles media compression for different formats"""
    
    @staticmethod
    async def compress_audio(input_path: str, output_path: str, target_bitrate: str = "128k") -> bool:
        """Compress audio file using ffmpeg"""
        try:
            cmd = [
                'ffmpeg', '-i', input_path,
                '-b:a', target_bitrate,
                '-vn',  # no video
                '-y',   # overwrite output
                output_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and os.path.exists(output_path):
                original_size = os.path.getsize(input_path)
                compressed_size = os.path.getsize(output_path)
                compression_ratio = (1 - compressed_size / original_size) * 100
                logger.info(f"Audio compressed: {original_size/1024/1024:.2f}MB -> {compressed_size/1024/1024:.2f}MB ({compression_ratio:.1f}% reduction)")
                return True
            else:
                logger.error(f"Audio compression failed: {stderr.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"Error in audio compression: {e}")
            return False
    
    @staticmethod
    async def compress_video(input_path: str, output_path: str, 
                           video_bitrate: str = "1M", audio_bitrate: str = "128k",
                           scale: str = "1280:720") -> bool:
        """Compress video file using ffmpeg"""
        try:
            cmd = [
                'ffmpeg', '-i', input_path,
                '-c:v', 'libx264',
                '-b:v', video_bitrate,
                '-c:a', 'aac',
                '-b:a', audio_bitrate,
                '-vf', f'scale={scale}:flags=lanczos',
                '-preset', 'medium',
                '-crf', '23',
                '-y',
                output_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and os.path.exists(output_path):
                original_size = os.path.getsize(input_path)
                compressed_size = os.path.getsize(output_path)
                compression_ratio = (1 - compressed_size / original_size) * 100
                logger.info(f"Video compressed: {original_size/1024/1024:.2f}MB -> {compressed_size/1024/1024:.2f}MB ({compression_ratio:.1f}% reduction)")
                return True
            else:
                logger.error(f"Video compression failed: {stderr.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"Error in video compression: {e}")
            return False
    
    @staticmethod
    async def convert_to_webm(input_path: str, output_path: str) -> bool:
        """Convert video to WebM format (better compression)"""
        try:
            cmd = [
                'ffmpeg', '-i', input_path,
                '-c:v', 'libvpx-vp9',
                '-b:v', '1M',
                '-c:a', 'libopus',
                '-b:a', '128k',
                '-f', 'webm',
                '-y',
                output_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            return process.returncode == 0 and os.path.exists(output_path)
            
        except Exception as e:
            logger.error(f"Error converting to WebM: {e}")
            return False
    
    @staticmethod
    def get_file_size_mb(file_path: str) -> float:
        """Get file size in MB"""
        return os.path.getsize(file_path) / (1024 * 1024)

class TelegramUploader:
    def __init__(self, bot_token: str, upload_channel: str):
        self.bot_token = bot_token
        self.upload_channel = upload_channel
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.session = None
        self.compressor = MediaCompressor()
        
    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def upload_file(self, file_path: str, video_id: str, media_type: str, caption: str = "") -> Dict:
        """Upload file to Telegram channel with compression if needed"""
        try:
            if not os.path.exists(file_path):
                return {"status": "error", "message": "File not found"}
            
            file_size = os.path.getsize(file_path)
            file_ext = os.path.splitext(file_path)[1].lower()
            
            # Check if file needs compression
            compressed_path = await self._compress_if_needed(file_path, media_type, file_size)
            if compressed_path and compressed_path != file_path:
                file_path = compressed_path
                file_size = os.path.getsize(file_path)
                file_ext = os.path.splitext(file_path)[1].lower()
                logger.info(f"Using compressed file: {file_path} ({file_size/1024/1024:.2f}MB)")
            
            session = await self.get_session()
            
            # Prepare form data
            data = aiohttp.FormData()
            data.add_field('chat_id', self.upload_channel)
            data.add_field('caption', f"{caption}\n\nVideo ID: {video_id}")
            
            file_name = os.path.basename(file_path)
            
            with open(file_path, 'rb') as file:
                # Choose upload method based on file type and size
                if media_type == "audio" or file_ext in ['.mp3', '.m4a', '.ogg', '.wav', '.webm']:
                    if file_size > TELEGRAM_FILE_LIMIT:
                        # Large audio files as document
                        data.add_field('document', file, filename=file_name)
                        api_method = "sendDocument"
                    else:
                        data.add_field('audio', file, filename=file_name)
                        api_method = "sendAudio"
                        
                elif media_type == "video" or file_ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm']:
                    if file_size > TELEGRAM_VIDEO_LIMIT:
                        # Large video files as document
                        data.add_field('document', file, filename=file_name)
                        api_method = "sendDocument"
                    else:
                        data.add_field('video', file, filename=file_name)
                        api_method = "sendVideo"
                else:
                    data.add_field('document', file, filename=file_name)
                    api_method = "sendDocument"
                
                # Make the API request with timeout
                try:
                    async with session.post(
                        f"{self.base_url}/{api_method}",
                        data=data,
                        timeout=aiohttp.ClientTimeout(total=300)  # 5 minute timeout
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            if result.get('ok'):
                                message = result['result']
                                file_info = self._extract_uploaded_file_info(message, media_type)
                                
                                # Clean up compressed file if it was created
                                if compressed_path and compressed_path != file_path:
                                    try:
                                        os.remove(compressed_path)
                                    except:
                                        pass
                                
                                return {
                                    "status": "success", 
                                    "message_id": message['message_id'],
                                    "file_info": file_info,
                                    "channel": self.upload_channel,
                                    "file_size_mb": file_size / (1024 * 1024),
                                    "compressed": compressed_path != file_path if compressed_path else False
                                }
                        else:
                            error_text = await response.text()
                            logger.error(f"Upload failed: {error_text}")
                            
                            # Clean up compressed file on error
                            if compressed_path and compressed_path != file_path:
                                try:
                                    os.remove(compressed_path)
                                except:
                                    pass
                            
                            if response.status == 413:
                                return {"status": "error", "message": "File too large even after compression"}
                            return {"status": "error", "message": f"Upload failed: {response.status}"}
                
                except asyncio.TimeoutError:
                    logger.error("Upload timeout")
                    return {"status": "error", "message": "Upload timeout"}
            
            return {"status": "error", "message": "Upload failed"}
            
        except Exception as e:
            logger.error(f"Error uploading file to Telegram: {e}")
            return {"status": "error", "message": f"Upload error: {str(e)}"}
    
    async def _compress_if_needed(self, file_path: str, media_type: str, file_size: int) -> str:
        """Compress file if it exceeds Telegram limits"""
        try:
            file_ext = os.path.splitext(file_path)[1].lower()
            file_limit = TELEGRAM_VIDEO_LIMIT if media_type == "video" else TELEGRAM_FILE_LIMIT
            
            if file_size <= file_limit:
                return file_path  # No compression needed
            
            logger.info(f"File too large ({file_size/1024/1024:.2f}MB), compressing...")
            
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                temp_path = temp_file.name
            
            success = False
            
            if media_type == "audio":
                # Try different bitrates for audio
                for bitrate in ["128k", "96k", "64k"]:
                    success = await self.compressor.compress_audio(file_path, temp_path, bitrate)
                    if success and os.path.getsize(temp_path) <= file_limit:
                        break
            
            elif media_type == "video":
                # Try WebM first (better compression)
                webm_path = temp_path + ".webm"
                if await self.compressor.convert_to_webm(file_path, webm_path):
                    if os.path.getsize(webm_path) <= file_limit:
                        os.rename(webm_path, temp_path)
                        success = True
                    else:
                        os.remove(webm_path)
                
                # If WebM not sufficient, try MP4 compression
                if not success:
                    for v_bitrate in ["1M", "800k", "500k"]:
                        success = await self.compressor.compress_video(file_path, temp_path, v_bitrate)
                        if success and os.path.getsize(temp_path) <= file_limit:
                            break
            
            if success and os.path.exists(temp_path):
                compressed_size = os.path.getsize(temp_path)
                logger.info(f"Compression successful: {compressed_size/1024/1024:.2f}MB")
                return temp_path
            else:
                # Clean up temp file if compression failed
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                return file_path  # Return original if compression fails
                
        except Exception as e:
            logger.error(f"Error in compression: {e}")
            return file_path  # Return original on error
    
    def _extract_uploaded_file_info(self, message: Dict, media_type: str) -> Dict:
        """Extract file information from upload response"""
        try:
            if media_type == "audio" and 'audio' in message:
                audio = message['audio']
                return {
                    'file_id': audio['file_id'],
                    'file_size': audio.get('file_size', 0),
                    'duration': audio.get('duration', 0),
                    'mime_type': audio.get('mime_type', 'audio/mpeg'),
                    'title': audio.get('title', ''),
                    'media_type': 'audio'
                }
            elif media_type == "video" and 'video' in message:
                video = message['video']
                return {
                    'file_id': video['file_id'],
                    'file_size': video.get('file_size', 0),
                    'duration': video.get('duration', 0),
                    'mime_type': video.get('mime_type', 'video/mp4'),
                    'media_type': 'video'
                }
            elif 'document' in message:
                document = message['document']
                return {
                    'file_id': document['file_id'],
                    'file_size': document.get('file_size', 0),
                    'mime_type': document.get('mime_type', ''),
                    'file_name': document.get('file_name', ''),
                    'media_type': 'document'
                }
            return {}
        except Exception as e:
            logger.error(f"Error extracting uploaded file info: {e}")
            return {}

class TelegramDBManager:
    def __init__(self, bot_token: str, db_channel: str):
        self.bot_token = bot_token
        self.db_channel = db_channel
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.session = None
        self.channel_cache = {}
        
    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def search_in_db_channel(self, video_id: str) -> Optional[Dict]:
        """Search for video_id in the database channel using username"""
        if video_id in self.channel_cache:
            cached_info, timestamp = self.channel_cache[video_id]
            if (time.time() - timestamp) < CACHE_TTL:
                return cached_info
        
        try:
            session = await self.get_session()
            
            # Search through channel messages for the video_id
            limit = 100
            async with session.get(
                f"{self.base_url}/getChatHistory",
                params={
                    "chat_id": self.db_channel,  # Using username directly
                    "limit": limit
                }
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        for message in data.get('result', []):
                            caption = message.get('caption', '') or message.get('text', '')
                            if video_id in caption:
                                file_info = self._extract_file_info(message, video_id)
                                if file_info:
                                    self.channel_cache[video_id] = (file_info, time.time())
                                    return file_info
            
            # Deep search if not found
            file_info = await self._deep_search_channel(video_id)
            if file_info:
                self.channel_cache[video_id] = (file_info, time.time())
                return file_info
                
            return None
            
        except Exception as e:
            logger.error(f"Error searching in DB channel: {e}")
            return None
    
    async def _deep_search_channel(self, video_id: str) -> Optional[Dict]:
        """Search more thoroughly in the channel using username"""
        try:
            session = await self.get_session()
            offset_id = 0
            max_searches = 500
            
            for _ in range(max_searches // 100):
                async with session.get(
                    f"{self.base_url}/getChatHistory",
                    params={
                        "chat_id": self.db_channel,  # Using username directly
                        "limit": 100,
                        "offset_id": offset_id
                    }
                ) as response:
                    if response.status != 200:
                        break
                        
                    data = await response.json()
                    if not data.get('ok') or not data.get('result'):
                        break
                    
                    messages = data['result']
                    for message in messages:
                        caption = message.get('caption', '') or message.get('text', '')
                        if video_id in caption:
                            file_info = self._extract_file_info(message, video_id)
                            if file_info:
                                return file_info
                    
                    if messages:
                        offset_id = messages[-1]['message_id']
                    else:
                        break
                        
            return None
            
        except Exception as e:
            logger.error(f"Error in deep search: {e}")
            return None
    
    def _extract_file_info(self, message: Dict, video_id: str) -> Optional[Dict]:
        """Extract file information from telegram message"""
        try:
            # Check for audio file
            if 'audio' in message:
                audio = message['audio']
                return {
                    'file_id': audio['file_id'],
                    'file_size': audio.get('file_size', 0),
                    'duration': audio.get('duration', 0),
                    'mime_type': audio.get('mime_type', 'audio/mpeg'),
                    'title': audio.get('title', f'audio_{video_id}'),
                    'message_id': message['message_id'],
                    'media_type': 'audio'
                }
            
            # Check for document
            elif 'document' in message:
                document = message['document']
                file_name = document.get('file_name', '')
                mime_type = document.get('mime_type', '')
                
                if 'audio' in mime_type or file_name.endswith('.webm') or file_name.endswith('.mp3'):
                    return {
                        'file_id': document['file_id'],
                        'file_size': document.get('file_size', 0),
                        'mime_type': mime_type,
                        'file_name': file_name,
                        'message_id': message['message_id'],
                        'media_type': 'audio'
                    }
                
                elif 'video' in mime_type or file_name.endswith('.mp4') or file_name.endswith('.mkv'):
                    return {
                        'file_id': document['file_id'],
                        'file_size': document.get('file_size', 0),
                        'mime_type': mime_type,
                        'file_name': file_name,
                        'message_id': message['message_id'],
                        'media_type': 'video'
                    }
            
            # Check for video file
            elif 'video' in message:
                video = message['video']
                return {
                    'file_id': video['file_id'],
                    'file_size': video.get('file_size', 0),
                    'duration': video.get('duration', 0),
                    'mime_type': video.get('mime_type', 'video/mp4'),
                    'message_id': message['message_id'],
                    'media_type': 'video'
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Error extracting file info: {e}")
            return None
    
    async def get_file_download_url(self, file_id: str) -> Optional[str]:
        """Get direct download URL for telegram file"""
        try:
            session = await self.get_session()
            async with session.get(
                f"{self.base_url}/getFile",
                params={"file_id": file_id}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        file_path = data['result']['file_path']
                        return f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            return None
        except Exception as e:
            logger.error(f"Error getting file URL: {e}")
            return None

class LocalDownloadManager:
    def __init__(self):
        self.session = None
        # Import the download utility
        try:
            from utils.downloader import download
            self.download = download
            self.downloader_available = True
            logger.info("Local downloader initialized successfully")
        except ImportError as e:
            logger.warning(f"Local downloader not available: {e}")
            self.downloader_available = False
        
    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def download_media(self, video_id: str, media_type: str) -> Dict:
        """Download using local downloader utility"""
        if not self.downloader_available:
            return {"status": "error", "message": "Local downloader not available"}
        
        try:
            # Use the local downloader
            file_path = await self.download(video_id, media_type)
            
            if file_path and os.path.exists(file_path):
                return {
                    "status": "success",
                    "file_path": file_path,
                    "video_id": video_id,
                    "media_type": media_type
                }
            else:
                return {"status": "error", "message": "Local download failed - file not found"}
                
        except Exception as e:
            logger.error(f"Error in local downloader: {e}")
            return {"status": "error", "message": f"Local download error: {e}"}

class DownloadManager:
    def __init__(self):
        self.local_downloader = LocalDownloadManager()
        self.telegram_db = TelegramDBManager(TELEGRAM_BOT_TOKEN, TELEGRAM_DB_CHANNEL) if TELEGRAM_BOT_TOKEN else None
        self.telegram_uploader = TelegramUploader(TELEGRAM_BOT_TOKEN, TELEGRAM_UPLOAD_CHANNEL) if TELEGRAM_BOT_TOKEN else None
        
    async def close(self):
        await self.local_downloader.close()
        if self.telegram_db:
            await self.telegram_db.close()
        if self.telegram_uploader:
            await self.telegram_uploader.close()
    
    async def download_media(self, video_id: str, media_type: str, request: Request = None) -> Dict:
        """
        Download media with LOCAL DOWNLOADER FIRST, then Telegram DB as fallback
        Returns immediately with download link, background upload runs separately
        """
        # FIRST: Try local downloader (priority)
        local_result = await self.local_downloader.download_media(video_id, media_type)
        
        if local_result["status"] == "success":
            file_path = local_result["file_path"]
            
            # Create a direct download URL for the local file
            if request:
                download_url = _make_full_link(request, "/file", video_id)
            else:
                download_url = f"/file/{video_id}?api={API_KEY}&type={media_type}"
                
            return {
                "status": "success",
                "type": "local_file", 
                "link": download_url,
                "file_path": file_path,
                "video_id": video_id,
                "source": "local_downloader"
            }
        
        # SECOND: Fallback to Telegram DB channel if local download fails
        if self.telegram_db:
            logger.info(f"Local download failed, trying Telegram DB for {video_id}")
            db_file = await self.telegram_db.search_in_db_channel(video_id)
            if db_file:
                file_url = await self.telegram_db.get_file_download_url(db_file['file_id'])
                if file_url:
                    return {
                        "status": "success",
                        "type": "db_channel",
                        "link": file_url,
                        "video_id": video_id,
                        "file_info": db_file,
                        "source": "telegram_db"
                    }
        
        # If both methods fail
        return {
            "status": "error", 
            "message": local_result.get("message", "Download failed from all sources")
        }
    
    async def upload_to_telegram_background(self, video_id: str, media_type: str, file_path: str):
        """Upload file to Telegram channel in background"""
        if not self.telegram_uploader:
            logger.info(f"Telegram uploader not configured, skipping upload for {video_id}")
            return
        
        try:
            logger.info(f"Starting background upload for {video_id}...")
            
            # Track upload start
            task_id = f"{video_id}_{media_type}"
            background_upload_tasks[task_id] = {"status": "uploading", "start_time": time.time()}
            
            caption = f"🎵 {media_type.title()} Download\nVideo ID: {video_id}"
            upload_result = await self.telegram_uploader.upload_file(
                file_path, video_id, media_type, caption
            )
            
            # Update task status
            if upload_result["status"] == "success":
                background_upload_tasks[task_id] = {
                    "status": "completed", 
                    "result": upload_result,
                    "completed_time": time.time()
                }
                logger.info(f"✅ Background upload completed for {video_id}")
            else:
                background_upload_tasks[task_id] = {
                    "status": "failed", 
                    "result": upload_result,
                    "completed_time": time.time()
                }
                logger.warning(f"❌ Background upload failed for {video_id}: {upload_result.get('message')}")
                
        except Exception as e:
            logger.error(f"Error in background upload for {video_id}: {e}")
            task_id = f"{video_id}_{media_type}"
            background_upload_tasks[task_id] = {
                "status": "error",
                "error": str(e),
                "completed_time": time.time()
            }

# Initialize Download Manager
download_manager = DownloadManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("TonyAPI starting up...")
    logger.info("Priority: Local Downloader → Telegram DB")
    logger.info(f"Upload Channel: {TELEGRAM_UPLOAD_CHANNEL}")
    yield
    # Shutdown
    logger.info("TonyAPI shutting down...")
    await download_manager.close()

app = FastAPI(title="TonyAPI", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Improved API key validation - using the simpler approach from reference
async def verify_api_key(api: str):
    """API key verification using the simpler reference approach"""
    if api != API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid API Key"
        )
    return True

def _make_full_link(request: Request, path: str, video_id: str):
    """Create full link using reference pattern"""
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}/{video_id}?api={API_KEY}"

@app.get("/")
async def root():
    return {
        "status": "TonyAPI is working", 
        "version": "2.1",
        "priority": "Local Downloader → Telegram DB",
        "upload_channel": TELEGRAM_UPLOAD_CHANNEL,
        "features": ["background_telegram_upload"]
    }

@app.get("/song/{video_id}")
async def song(video_id: str, request: Request, background_tasks: BackgroundTasks, api: str = Depends(verify_api_key), upload: bool = True):
    """Get audio download info - with optional background Telegram upload"""
    logger.info(f"Song request - Video ID: {video_id}, Upload: {upload}")
    
    try:
        # Download the file first (immediate response)
        download_info = await download_manager.download_media(video_id, "audio", request)
        
        if download_info["status"] == "success":
            response_data = {
                "status": "done", 
                "link": download_info["link"],
                "type": download_info.get("type", "stream"),
                "video_id": video_id,
                "source": download_info.get("source", "unknown")
            }
            
            # Start background upload if requested and we have a local file
            if upload and download_info.get("source") == "local_downloader" and "file_path" in download_info:
                background_tasks.add_task(
                    download_manager.upload_to_telegram_background,
                    video_id, "audio", download_info["file_path"]
                )
                response_data["background_upload"] = "started"
                response_data["upload_task_id"] = f"{video_id}_audio"
            
            return JSONResponse(response_data)
        else:
            return JSONResponse({
                "status": "error", 
                "message": download_info.get("message", "Download failed")
            }, status_code=500)
            
    except Exception as e:
        logger.error(f"Error in song endpoint: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)}, 
            status_code=500
        )

@app.get("/video/{video_id}")
async def video(video_id: str, request: Request, background_tasks: BackgroundTasks, api: str = Depends(verify_api_key), upload: bool = True):
    """Get video download info - with optional background Telegram upload"""
    logger.info(f"Video request - Video ID: {video_id}, Upload: {upload}")
    
    try:
        # Download the file first (immediate response)
        download_info = await download_manager.download_media(video_id, "video", request)
        
        if download_info["status"] == "success":
            response_data = {
                "status": "done", 
                "link": download_info["link"],
                "type": download_info.get("type", "stream"),
                "video_id": video_id,
                "source": download_info.get("source", "unknown")
            }
            
            # Start background upload if requested and we have a local file
            if upload and download_info.get("source") == "local_downloader" and "file_path" in download_info:
                background_tasks.add_task(
                    download_manager.upload_to_telegram_background,
                    video_id, "video", download_info["file_path"]
                )
                response_data["background_upload"] = "started"
                response_data["upload_task_id"] = f"{video_id}_video"
            
            return JSONResponse(response_data)
        else:
            return JSONResponse({
                "status": "error", 
                "message": download_info.get("message", "Download failed")
            }, status_code=500)
            
    except Exception as e:
        logger.error(f"Error in video endpoint: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)}, 
            status_code=500
        )

# File serving endpoint for locally downloaded files
@app.get("/file/{video_id}")
async def serve_file(video_id: str, type: str = "audio", api: str = Depends(verify_api_key)):
    """Serve locally downloaded files"""
    try:
        # Construct expected file path based on your downloader's output
        filename = f"{video_id}.{'mp3' if type == 'audio' else 'mp4'}"
        file_path = os.path.join(DOWNLOAD_FOLDER, filename)
        
        # Alternative naming patterns
        if not os.path.exists(file_path):
            # Try other possible naming conventions
            for ext in ['.mp3', '.mp4', '.webm', '.m4a']:
                alt_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}{ext}")
                if os.path.exists(alt_path):
                    file_path = alt_path
                    break
        
        if os.path.exists(file_path):
            return FileResponse(
                file_path,
                media_type="audio/mpeg" if type == "audio" else "video/mp4",
                filename=filename
            )
        else:
            raise HTTPException(status_code=404, detail="File not found")
            
    except Exception as e:
        logger.error(f"Error serving file: {e}")
        raise HTTPException(status_code=500, detail="Error serving file")

# Main download endpoint with background upload option
@app.get("/download")
async def download_endpoint(
    url: str, 
    type: str = "audio",
    upload: bool = True,
    request: Request = None,
    background_tasks: BackgroundTasks = None,
    api: str = Depends(verify_api_key)
):
    """
    Main download endpoint with LOCAL DOWNLOADER priority and background Telegram upload
    """
    logger.info(f"Download request - URL: {url}, Type: {type}, Upload: {upload}")
    
    # Extract video ID from URL or use as-is
    video_id = url
    if 'v=' in url:
        video_id = url.split('v=')[-1].split('&')[0]
    elif 'youtu.be/' in url:
        video_id = url.split('youtu.be/')[-1].split('?')[0]
    elif 'youtube.com/watch' in url:
        video_id = url.split('v=')[-1].split('&')[0]
    
    # Clean video ID
    video_id = video_id.split('?')[0].split('&')[0]
    
    if not video_id or len(video_id) < 3:
        return JSONResponse(
            {"status": "error", "message": "Invalid video ID"}, 
            status_code=400
        )
    
    # Validate media type
    if type not in ["audio", "video"]:
        return JSONResponse(
            {"status": "error", "message": "Type must be 'audio' or 'video'"}, 
            status_code=400
        )
    
    try:
        # Download the file first (immediate response)
        download_info = await download_manager.download_media(video_id, type, request)
        logger.info(f"Download result for {video_id}: {download_info.get('status')} (Source: {download_info.get('source', 'unknown')})")
        
        # Start background upload if requested and we have a local file
        if upload and download_info.get("status") == "success" and download_info.get("source") == "local_downloader" and "file_path" in download_info:
            if background_tasks:
                background_tasks.add_task(
                    download_manager.upload_to_telegram_background,
                    video_id, type, download_info["file_path"]
                )
                download_info["background_upload"] = "started"
                download_info["upload_task_id"] = f"{video_id}_{type}"
        
        return JSONResponse(download_info)
            
    except Exception as e:
        logger.error(f"Error in download endpoint: {e}")
        return JSONResponse(
            {"status": "error", "message": f"Internal server error: {str(e)}"}, 
            status_code=500
        )

# Health check endpoint
@app.get("/health")
async def health_check():
    db_cache_size = len(download_manager.telegram_db.channel_cache) if download_manager.telegram_db else 0
    active_uploads = {k: v for k, v in background_upload_tasks.items() if v.get("status") == "uploading"}
    
    return {
        "status": "healthy",
        "local_downloader_available": download_manager.local_downloader.downloader_available,
        "db_channel_enabled": download_manager.telegram_db is not None,
        "upload_channel_enabled": download_manager.telegram_uploader is not None,
        "upload_channel": TELEGRAM_UPLOAD_CHANNEL,
        "db_cache_size": db_cache_size,
        "active_background_uploads": len(active_uploads),
        "total_background_tasks": len(background_upload_tasks),
        "priority_order": "Local Downloader → Telegram DB",
        "timestamp": time.time()
    }

# Background upload status endpoint
@app.get("/upload/status/{task_id}")
async def get_upload_status(task_id: str, api: str = Depends(verify_api_key)):
    """Get status of background upload task"""
    if task_id in background_upload_tasks:
        return {
            "task_id": task_id,
            "status": background_upload_tasks[task_id]
        }
    else:
        return {
            "task_id": task_id,
            "status": "not_found",
            "message": "Task ID not found"
        }

# DB Channel management endpoints
@app.get("/db/search/{video_id}")
async def search_db_channel(video_id: str, api: str = Depends(verify_api_key)):
    """Search for file in database channel"""
    if not download_manager.telegram_db:
        raise HTTPException(status_code=400, detail="Database channel not configured")
    
    file_info = await download_manager.telegram_db.search_in_db_channel(video_id)
    if file_info:
        return {"status": "found", "file_info": file_info}
    return {"status": "not_found"}

@app.post("/db/refresh_cache")
async def refresh_db_cache(api: str = Depends(verify_api_key)):
    """Refresh database channel cache"""
    if download_manager.telegram_db:
        download_manager.telegram_db.channel_cache.clear()
        return {"status": "success", "message": "DB cache cleared"}
    return {"status": "error", "message": "DB channel not configured"}

# Upload management endpoints
@app.post("/upload/{video_id}")
async def upload_file_directly(
    video_id: str, 
    type: str = "audio", 
    background_tasks: BackgroundTasks = None,
    api: str = Depends(verify_api_key)
):
    """Manually upload a file to Telegram channel in background"""
    if not download_manager.telegram_uploader:
        raise HTTPException(status_code=400, detail="Upload channel not configured")
    
    try:
        # First download the file
        local_result = await download_manager.local_downloader.download_media(video_id, type)
        if local_result["status"] != "success":
            return JSONResponse(
                {"status": "error", "message": "Download failed before upload"}, 
                status_code=400
            )
        
        # Start background upload
        if background_tasks:
            background_tasks.add_task(
                download_manager.upload_to_telegram_background,
                video_id, type, local_result["file_path"]
            )
            
            return {
                "status": "upload_started",
                "task_id": f"{video_id}_{type}",
                "message": "Background upload started"
            }
        else:
            return JSONResponse(
                {"status": "error", "message": "Background tasks not available"}, 
                status_code=500
            )
        
    except Exception as e:
        logger.error(f"Error in manual upload: {e}")
        return JSONResponse(
            {"status": "error", "message": f"Upload failed: {str(e)}"}, 
            status_code=500
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    
    uvicorn.run(
        app, 
        host="0.0.0.0",
        port=port,
        workers=1,
        loop="asyncio",
        access_log=True,
        log_level="info"
    )
