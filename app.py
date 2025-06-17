# app.py

from flask import Flask, request, jsonify
from pytubefix import YouTube
from dotenv import load_dotenv
import os
import re
import tempfile
import shutil
import boto3
import logging
from pytubefix import YouTube, request
import traceback

# Load environment variables from .env file (for local development)
load_dotenv()

app = Flask(__name__)

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration loaded from environment variables ---
API_KEY = os.getenv("API_KEY")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION")
# The port is now read from .env or provided by the hosting environment (like Render)
APP_PORT = os.getenv("PORT")

# This variable is for your CLIENT app to know where to call THIS server.
# This server's code itself doesn't directly use APP_URL for S3 download links.
# S3_DOWNLOAD_BASE_URL = os.getenv("APP_URL") # Not needed for S3 pre-signed URLs

# --- S3 Client Setup ---
s3_client = None
if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET_NAME, AWS_DEFAULT_REGION]):
    logger.error("Missing one or more AWS S3 environment variables! S3 operations will be disabled..")
else:
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_DEFAULT_REGION
        )
        logger.info(f"S3 client initialized for bucket: {S3_BUCKET_NAME} in region: {AWS_DEFAULT_REGION}")
    except Exception as e:
        logger.error(f"Could not initialize S3 client with provided credentials: {e}")
        logger.error(traceback.format_exc())
        s3_client = None


def get_video_data_and_real_link(youtube_url):
    """
    Extracts YouTube video data, downloads the video, uploads it to S3,
    and generates a real, temporary download link.
    """
    temp_dir = None
    video_path_on_server = None
    
    try:
        if not s3_client:
            raise Exception("S3 client not initialized. Check AWS credentials and S3 bucket configuration.")
        # Spoof the headers to avoid being blocked
        request.default_headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/113.0.0.0 Safari/537.36"
        )

        yt = YouTube(youtube_url, use_po_token=True)


        yt.check_availability()

        sanitized_title = re.sub(r'[^\w\s-]', '', yt.title).replace(' ', '_')
        s3_object_key = f"youtube_downloads/{sanitized_title}_{yt.video_id}.mp4"
        
        logger.info(f"Attempting to download for S3 object key: {s3_object_key}")

        temp_dir = tempfile.mkdtemp()
        
        stream_to_download = yt.streams.filter(progressive=True, file_extension="mp4").order_by('resolution').desc().first()
        
        if stream_to_download and int(stream_to_download.resolution[:-1]) >= 360:
            logger.info(f"Downloading combined stream ({stream_to_download.resolution})...")
            temp_filename = f"{yt.video_id}_combined.mp4"
            video_path_on_server = os.path.join(temp_dir, temp_filename)
            stream_to_download.download(output_path=temp_dir, filename=temp_filename)
            logger.info(f"Downloaded combined stream to: {os.path.basename(video_path_on_server)}")
        else:
            logger.info("Falling back to separate video/audio download for higher quality (requires FFmpeg).")
            video_stream = yt.streams.filter(res="1080p", file_extension="mp4", only_video=True).first()
            audio_stream = yt.streams.filter(only_audio=True, file_extension="mp4").order_by('abr').desc().first()

            if not video_stream or not audio_stream:
                raise Exception("Could not find suitable 1080p video or audio stream for merging. Try a lower quality or a different video.")

            video_temp_file = os.path.join(temp_dir, f"{yt.video_id}_video.mp4")
            audio_temp_file = os.path.join(temp_dir, f"{yt.video_id}_audio.mp4")
            video_path_on_server = os.path.join(temp_dir, f"{yt.video_id}_merged.mp4")

            logger.info(f"Downloading video stream to: {os.path.basename(video_temp_file)}")
            video_stream.download(output_path=temp_dir, filename=f"{yt.video_id}_video.mp4")
            logger.info(f"Downloading audio stream to: {os.path.basename(audio_temp_file)}")
            audio_stream.download(output_path=temp_dir, filename=f"{yt.video_id}_audio.mp4")

            logger.info(f"Merging video and audio with FFmpeg to: {os.path.basename(video_path_on_server)}")
            ffmpeg_command = f'ffmpeg -y -i "{video_temp_file}" -i "{audio_temp_file}" -c:v copy -c:a aac "{video_path_on_server}"'
            os.system(ffmpeg_command)
            
            os.remove(video_temp_file)
            os.remove(audio_temp_file)
            logger.info("Temporary video and audio files removed after merging.")

        if not video_path_on_server or not os.path.exists(video_path_on_server):
            raise Exception("Video download or merge failed; no final video file found on server.")

        # --- Upload to S3 ---
        logger.info(f"Uploading {os.path.basename(video_path_on_server)} to S3 bucket {S3_BUCKET_NAME} as {s3_object_key}")
        s3_client.upload_file(video_path_on_server, S3_BUCKET_NAME, s3_object_key)
        logger.info("Upload complete.")

        # --- Generate Pre-Signed S3 URL (valid for 1 hour) ---
        download_link = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET_NAME, 'Key': s3_object_key},
            ExpiresIn=3600 # URL valid for 1 hour (adjust as needed)
        )
        logger.info(f"Generated pre-signed URL successfully.")

        # --- Extract other metadata ---
        available_qualities = []
        for stream in yt.streams.filter(file_extension="mp4", progressive=True).order_by('resolution').desc():
            if stream.resolution:
                available_qualities.append(f"{stream.resolution} (combined)")
        for stream in yt.streams.filter(only_video=True, file_extension="mp4").order_by('resolution').desc():
            if stream.resolution:
                available_qualities.append(f"{stream.resolution} (video only)")
        for stream in yt.streams.filter(only_audio=True, file_extension="mp4").order_by('abr').desc():
            if stream.abr:
                available_qualities.append(f"{stream.abr} (audio only)")
        available_qualities = sorted(list(set(available_qualities)), 
                                     key=lambda x: (
                                         int(re.search(r'(\d+)', x).group(1)) if re.search(r'(\d+)', x) else 0,
                                         'combined' in x, 'video only' in x, 'audio only' in x
                                     ), reverse=True)

        return {
            "status": "success",
            "video_title": yt.title,
            "video_thumbnail_url": yt.thumbnail_url,
            "video_description": yt.description,
            "video_length_seconds": yt.length,
            "video_views": yt.views,
            "author": yt.author,
            "publish_date": str(yt.publish_date),
            "keywords": yt.keywords if yt.keywords else [],
            "available_qualities": available_qualities,
            "download_link": download_link,
            "message": "Video downloaded, uploaded to S3, and pre-signed URL generated."
        }
    except Exception as e:
        logger.error(f"Error processing YouTube link {youtube_url}: {e}")
        return {
            "status": "error",
            "message": f"Failed to process YouTube link: {str(e)}. Check server logs for details. Ensure FFmpeg is installed if high quality is desired, and AWS credentials/S3 config are correct.",
            "youtube_url": youtube_url
        }
    finally:
        if temp_dir and os.path.exists(temp_dir):
            logger.info(f"Cleaning up temporary directory: {temp_dir}")
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.error(f"Error removing temporary directory {temp_dir}: {e}")

@app.route('/download_youtube_data', methods=['POST'])
def download_youtube_data():
    """
    API endpoint to receive a YouTube URL and return video data + download link.
    Requires an 'X-API-Key' header for authentication.
    """
    auth_header = request.headers.get('X-API-Key')
    if API_KEY and auth_header != API_KEY:
        logger.warning("Unauthorized access attempt due to invalid API Key.")
        return jsonify({"status": "error", "message": "Unauthorized: Invalid API Key"}), 401
    
    data = request.get_json()
    if not data or 'youtube_url' not in data:
        logger.warning("Bad request: Missing 'youtube_url' in request body.")
        return jsonify({"status": "error", "message": "Missing 'youtube_url' in request body."}), 400

    youtube_url = data['youtube_url']
    
    if not re.match(r"^(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+", youtube_url):
        logger.warning(f"Invalid YouTube URL format received: {youtube_url}")
        return jsonify({"status": "error", "message": "Invalid YouTube URL format."}), 400

    logger.info(f"Processing request for YouTube URL: {youtube_url}")
    result = get_video_data_and_real_link(youtube_url)
    
    if result.get("status") == "error":
        logger.error(f"Request failed for {youtube_url}: {result.get('message')}")
        return jsonify(result), 500
    else:
        logger.info(f"Successfully processed {youtube_url}.")
        return jsonify(result), 200