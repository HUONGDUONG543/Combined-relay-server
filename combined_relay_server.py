#!/usr/bin/env python3
"""
combined_relay_server.py
============================================================================
Relay server DUY NHẤT cho firmware T-Display S3 Video Player - gộp
tiktok_relay_server.py + yt_relay_server.py thành 1 file, chạy CHUNG 1
cổng (mặc định 8000) để .ino chỉ cần trỏ về đúng 1 host cho cả TikTok
lẫn YouTube (không còn RELAY_HOST_HOME/AWAY và YT_RELAY_HOST_HOME/AWAY
tách riêng nữa - xem RELAY_HOST_HOME/AWAY hợp nhất trong file .ino).

Cung cấp cả 3 endpoint mà firmware gọi tới:

  GET /search?q=<từ khóa>&n=<số kết quả>
      -> [ { "id": "<video id>", "title": "...", "duration": "3:32" }, ... ]
      (YouTube search qua yt-dlp - TikTok không có endpoint search, xem
      /random bên dưới)

  GET /stream?url=<youtube url / ytsearch1:... / link TikTok cụ thể>&w=&h=&height_cap=&fps=
      -> resolve link đó bằng yt-dlp rồi transcode sang AVI (MJPEG + PCM)
      cho firmware phát - dùng CHUNG 1 logic cho cả 2 nền tảng, vì yt-dlp
      tự nhận diện domain trong URL và xử lý đúng cách tương ứng.

  GET /random
      -> trả về {"url": "<1 link random>"} lấy từ PLAYLIST_FILE (mỗi dòng
      1 link TikTok, tự soạn - xem hướng dẫn dưới). Rỗng/không tìm thấy
      file -> 404.

Cách dùng playlist TikTok: tự mở TikTok, copy link "Share" của mỗi video
muốn có trong playlist, thêm mỗi link 1 dòng vào file PLAYLIST_FILE (mặc
định "tiktok_links.txt", cùng thư mục với file này).

CHẠY TRÊN TERMUX:
  pkg install python ffmpeg
  pip install flask yt-dlp
  termux-wake-lock   (cần thêm: pkg install termux-api - giữ CPU không bị
                       Android hạ xung khi chạy nền, tắt luôn tối ưu pin
                       cho Termux trong Cài đặt > Ứng dụng > Termux > Pin
                       > Không giới hạn)
  python combined_relay_server.py

  [yt-dlp-SABR fix] YouTube liên tục siết định dạng "SABR-only" theo từng
  client (android/web/ios/...) - khi bị chặn hết định dạng nhỏ (adaptive),
  yt-dlp phải rơi về itag 18 (360p GỘP SẴN, full độ dài gốc - có video ca
  nhạc lên tới vài trăm MB) và YouTube thường siết tốc độ tải itag này với
  bên thứ ba xuống rất thấp (~50-60KB/s trong thực tế đo được) - không đủ
  để ffmpeg giải mã+mã hoá lại kịp, gây hiện tượng phát 1-2 giây rồi phải
  chờ tải. Đây là hạn chế phía YouTube, KHÔNG phải do buffer/code relay
  này - xem resolve_stream_urls() bên dưới đã thử mở rộng danh sách client
  dự phòng để tăng khả năng vẫn còn client nào đó chưa bị SABR chặn cho
  đúng video đang phát, nhưng không phải lúc nào cũng giải quyết được -
  cách chắc chắn hơn là cài PO Token provider (ví dụ bgutil-ytdlp-pot-
  provider, đã hỗ trợ Termux) để mở lại được các định dạng adaptive nhỏ.
  Vì yt-dlp/YouTube đổi cách xử lý liên tục, nhớ cập nhật yt-dlp thường
  xuyên: pip install -U yt-dlp
  (mặc định lắng nghe 0.0.0.0:8000 - dán IP LAN hoặc link cloudflared vào
   RELAY_HOST_HOME/AWAY trong file .ino - CHỈ 1 cặp macro cho cả 2 nền
   tảng, không còn YT_RELAY_HOST_HOME/AWAY riêng nữa)

  Không còn chạy song song 2 file cũ (tiktok_relay_server.py +
  yt_relay_server.py) nữa - file này thay thế CẢ HAI. Có thể xoá 2 file
  cũ hoặc giữ lại làm tham khảo, không ảnh hưởng gì vì .ino giờ chỉ gọi
  1 host duy nhất.
============================================================================
"""

import subprocess
import shlex
import random
import os
from flask import Flask, request, Response, jsonify
import yt_dlp
from werkzeug.serving import WSGIRequestHandler

app = Flask(__name__)

PORT = 8000  # 1 cổng duy nhất cho cả /search, /stream (YouTube + TikTok), /random

# File danh sách link cho /random - tự soạn, mỗi dòng 1 link TikTok, dòng
# trống hoặc bắt đầu bằng "#" bị bỏ qua (dùng để ghi chú). Cùng thư mục với
# file này trừ khi bạn đổi thành đường dẫn tuyệt đối.
PLAYLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_links.txt")


# Ép HTTP/1.0 để tránh "Transfer-Encoding: chunked" - board đọc socket thô,
# không tự giải mã chunked-encoding của HTTP/1.1, nên header hex chunk-size
# bị lẫn vào đầu dữ liệu AVI làm sai lệch parseAviNet(). HTTP/1.0 gửi dữ
# liệu thô, đóng kết nối khi hết - không còn framing lạ chen vào.
class HTTP10RequestHandler(WSGIRequestHandler):
    protocol_version = "HTTP/1.0"


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@app.route("/search")
def search():
    query = request.args.get("q", "")
    n = int(request.args.get("n", 5))
    if not query:
        return jsonify([])

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
            for entry in info.get("entries", []):
                if not entry:
                    continue
                results.append({
                    "id": entry.get("id", ""),
                    "title": entry.get("title", ""),
                    "duration": format_duration(entry.get("duration")),
                })
    except Exception as e:
        print(f"[search] error: {e}")
        return jsonify([])

    return jsonify(results)


def format_ffmpeg_headers(fmt):
    """Chuyển dict http_headers mà yt-dlp gắn theo từng format thành chuỗi
    header ffmpeg hiểu (mỗi dòng 'Key: Value', kết bằng \\r\\n) - dùng với
    cờ -headers trước mỗi -i. Không có bước này, ffmpeg tải trực tiếp URL
    CDN mà KHÔNG có User-Agent/header khớp với phiên đã resolve - nguồn
    thường vẫn cho phép mở kết nối (200 OK) nhưng cắt giữa chừng sau vài
    giây vì thấy request "không giống" trình phát hợp lệ."""
    headers = fmt.get("http_headers") or {}
    if not headers:
        return ""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


def resolve_stream_urls(video_url, height_cap):
    """Dùng yt-dlp lấy URL luồng trực tiếp - dùng CHUNG cho cả YouTube lẫn
    TikTok, yt-dlp tự nhận diện domain trong video_url và xử lý đúng cách
    tương ứng. Đòi "bestvideo+bestaudio" (2 luồng tách biệt) rồi để ffmpeg
    tự ghép khi mux, vì phần lớn nguồn không còn phát format gộp sẵn.
    Trả về (video_url, audio_url, video_headers, audio_headers) - audio_url
    có thể None nếu video đó hiếm hoi vẫn có format gộp sẵn."""
    fmt = (
        f"bestvideo[height<={height_cap}]+bestaudio"
        f"/best[height<={height_cap}]"
        f"/best"
    )
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "skip_download": True,
        # [yt-dlp-SABR fix] YouTube đang siết "SABR-only" theo TỪNG client
        # riêng lẻ (không phải toàn bộ YouTube) - một video có thể bị chặn
        # định dạng nhỏ ở client này nhưng vẫn còn ở client khác, và việc
        # đó thay đổi theo thời gian/video. Trước chỉ thử "android" rồi
        # "web" - giờ thêm "tv" và "ios" vào danh sách dự phòng để tăng cơ
        # hội gặp đúng client chưa bị chặn cho video đang phát (thứ tự này
        # không đảm bảo luôn tránh được SABR - khi CẢ danh sách đều bị chặn
        # định dạng nhỏ, fmt vẫn phải rơi xuống "best" không giới hạn kích
        # thước, tức itag 18 360p gộp sẵn, xem ghi chú SABR ở đầu file).
        # Muốn mở lại được định dạng nhỏ một cách ổn định, cần PO Token
        # provider (bgutil-ytdlp-pot-provider) chứ list client dự phòng chỉ
        # là biện pháp "hên xui" không tốn thêm hạ tầng.
        "extractor_args": {"youtube": {"player_client": ["android", "tv", "ios", "web"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        if "entries" in info:  # ytsearch1:... trả về playlist 1 phần tử
            info = info["entries"][0]
        formats = info.get("requested_formats")
        if formats:
            video_fmt = next((f for f in formats if f.get("vcodec") not in (None, "none")), formats[0])
            audio_fmt = next((f for f in formats if f.get("acodec") not in (None, "none")), None)
            if audio_fmt and audio_fmt is video_fmt:
                audio_fmt = None
            video_url_out = video_fmt["url"]
            video_headers = format_ffmpeg_headers(video_fmt)
            audio_url_out = audio_fmt["url"] if audio_fmt else None
            audio_headers = format_ffmpeg_headers(audio_fmt) if audio_fmt else ""
            return video_url_out, audio_url_out, video_headers, audio_headers
        # Trường hợp hiếm: yt-dlp tự tìm được 1 format gộp sẵn duy nhất
        return info["url"], None, format_ffmpeg_headers(info), ""


def load_playlist():
    """Đọc PLAYLIST_FILE, trả về list các link (đã bỏ dòng trống/comment)."""
    if not os.path.exists(PLAYLIST_FILE):
        return []
    with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    return [ln for ln in lines if ln and not ln.startswith("#")]


@app.route("/random")
def random_link():
    links = load_playlist()
    if not links:
        return jsonify({
            "error": f"Playlist rỗng hoặc chưa có file {os.path.basename(PLAYLIST_FILE)} - "
                     f"thêm mỗi link TikTok 1 dòng vào file đó rồi thử lại."
        }), 404
    return jsonify({"url": random.choice(links)})


@app.route("/stream")
def stream():
    video_url = request.args.get("url", "")
    w = request.args.get("w", "320")
    h = request.args.get("h", "170")
    height_cap = request.args.get("height_cap", "360")
    fps = request.args.get("fps", "15")

    if not video_url:
        return Response(status=400)

    try:
        video_direct_url, audio_direct_url, video_headers, audio_headers = resolve_stream_urls(video_url, height_cap)
    except Exception as e:
        print(f"[stream] resolve error: {e}")
        return Response(status=502)

    # "-re" ĐÃ BỎ (thử nghiệm): bug gốc là tràn số uint32_t trong
    # parseAviNet() (đã sửa ở file .ino), không phải do thiếu "-re". Ép
    # pace real-time trên 2 input riêng biệt (video/audio) khiến độ trễ
    # mạng dao động khi tải nguồn truyền thẳng thành giật đồng thời cả
    # hình lẫn tiếng. Nếu lỗi "Stream error / disconnected" tái xuất hiện,
    # khôi phục "-re" trước mỗi -i.
    #
    # [yt-dlp-SABR fix] "-reconnect..." thêm trước MỖI -i: khi nguồn bị
    # YouTube siết tốc độ (itag 18 fallback do SABR, xem ghi chú ở đầu
    # file) hoặc rớt mạng giữa chừng, log thực tế cho thấy kết nối HTTP bị
    # "Connection reset by peer" - không có các cờ này, ffmpeg coi đó là
    # lỗi input và toàn bộ luồng chết luôn (muxer "Broken pipe" phía sau,
    # đúng như log). Các cờ dưới cho ffmpeg TỰ kết nối lại (kể cả khi lỗi
    # xảy ra giữa stream, không chỉ lúc mở kết nối) thay vì bỏ cuộc ngay -
    # không giải quyết được tốc độ nguồn đang bị siết, nhưng đỡ việc cả
    # phiên phát bị chết cứng chỉ vì 1 lần rớt mạng thoáng qua.
    RECONNECT_ARGS = (
        "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
        "-reconnect_delay_max 5 "
    )
    video_headers_arg = f"-headers {shlex.quote(video_headers)} " if video_headers else ""
    audio_headers_arg = f"-headers {shlex.quote(audio_headers)} " if audio_headers else ""

    # [perf] -threads 0: để ffmpeg tự chọn số luồng bằng số nhân CPU của máy
    # (mặc định ffmpeg chỉ dùng 1 luồng cho phần lớn filter/encode nếu không
    # set cờ này). mjpeg là codec intra-only (mỗi frame độc lập) nên encode
    # scale-song-song-nhiều-frame rất hiệu quả trên CPU đa nhân của điện
    # thoại - đây là chỗ có khả năng cao nhất đang là nút thắt CPU thực sự
    # (không phải mạng - đã đo mạng nhà đủ nhanh), vì trước đó ffmpeg chỉ
    # chạy 1 nhân trong khi máy có 6-8 nhân rảnh.
    THREADS_ARG = "-threads 0 "

    if audio_direct_url:
        cmd = (
            f"ffmpeg -v error "
            f"{RECONNECT_ARGS}{video_headers_arg}-i {shlex.quote(video_direct_url)} "
            f"{RECONNECT_ARGS}{audio_headers_arg}-i {shlex.quote(audio_direct_url)} "
            f"-map 0:v:0 -map 1:a:0 "
            f"{THREADS_ARG}"
            f"-vf scale={w}:{h},fps={fps} "
            # [perf] q:v 20 (tăng từ 16, tăng từ 12 gốc): log Serial cho
            # thấy đỉnh (max) của videoPayloadRead/drawJpg cao gấp 3-4 lần
            # trung bình - đúng lúc cảnh có nhiều chuyển động/chi tiết, khung
            # JPEG lúc đó nặng hơn hẳn khiến cả encode lẫn decode khựng lại
            # đột ngột (giật nặng đúng khi hành động nhanh). Nén thêm ở mọi
            # khung (kể cả khung tĩnh) để hạ luôn kích thước khung "khó",
            # giảm biên độ đỉnh - đổi lại hình mờ hơn 1 chút liên tục.
            f"-c:v mjpeg -q:v 20 "
            # 16000Hz mono 16-bit (~32000 Bps) thay vì 22050/44100Hz: máy
            # Termux không đủ CPU/băng thông để encode/tải kịp mức cao hơn,
            # gây underrun audio định kỳ. Firmware tự đọc audioRate/audioBits
            # từ AVI header ffmpeg tạo ra, không hardcode ở .ino.
            f"-c:a pcm_s16le -ar 16000 -ac 1 "
            f"-f avi pipe:1"
        )
    else:
        cmd = (
            f"ffmpeg -v error {RECONNECT_ARGS}{video_headers_arg}-i {shlex.quote(video_direct_url)} "
            f"{THREADS_ARG}"
            f"-vf scale={w}:{h},fps={fps} "
            f"-c:v mjpeg -q:v 20 "  # [perf] tăng từ 16, xem giải thích ở nhánh có audio phía trên
            f"-c:a pcm_s16le -ar 16000 -ac 1 "
            f"-f avi pipe:1"
        )

    # [perf] bufsize=1<<20 (1MB) thay vì mặc định: Python mặc định dùng
    # buffer I/O khá nhỏ cho pipe, khiến vòng đọc bên dưới phải chờ ffmpeg
    # từng đợt ngắn thay vì có sẵn dữ liệu để đọc liên tục - đặt lớn hơn để
    # generate() ít bị đói dữ liệu khi ffmpeg encode dồn cụm (do CPU đa
    # nhân giờ xử lý nhanh hơn nhưng không đều).
    proc = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, bufsize=1 << 20)

    # [perf] Đọc 64KB/lần thay vì 4KB: giảm số lần gọi syscall read() (mỗi
    # lần đọc nhỏ tốn thêm chi phí chuyển ngữ cảnh Python<->OS khi encode
    # đang chạy nhanh, dồn dữ liệu). Vẫn forward NGAY từng chunk đọc được
    # (không gộp thêm ở tầng ứng dụng) nên độ trễ audio/video giữa các
    # chunk không đổi - chỉ đổi kích thước lần đọc, không đổi cách stream.
    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            proc.terminate()

    return Response(generate(), mimetype="video/avi")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
