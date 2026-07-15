# scripts and resources to setup a potato server

assuming its a pi4

so I'll need automated script installs for

- python race project
  - staging script (copies down code, setup prerequisites)
  - running script
- write a pygame grabber
  - acts like the drawing pane for the pygame thing
  - stream to youtube


ex grabber:

````py
import os
import subprocess
import pygame

# 1. Force Pygame to run headlessly in system memory
os.environ["SDL_VIDEODRIVER"] = "dummy"

pygame.init()
WIDTH, HEIGHT = 1280, 720
screen = pygame.display.set_mode((WIDTH, HEIGHT))
clock = pygame.time.Clock()

# 2. Launch FFmpeg as a background subprocess targeting YouTube

YOUTUBE_STREAM_KEY = "your-youtube-stream-key-here"
ffmpeg_cmd = [
    "ffmpeg",
    "-y",
    "-f", "rawvideo",
    "-vcodec", "rawvideo",
    "-pix_fmt", "rgba",
    "-s", f"{WIDTH}x{HEIGHT}",
    "-r", "30",                         # Match your game loop FPS
    "-i", "-",                          # Read frames from Python stdin
    "-c:v", "libx264",                  # Encode to H.264
    "-pix_fmt", "yuv420p",
    "-preset", "veryfast",
    "-f", "flv",
    f"rtmp://://youtube.com{YOUTUBE_STREAM_KEY}"
]

ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)

running = True
while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

    # --- Your Game Logic & Drawing Here ---
    screen.fill((0, 0, 255)) 
    pygame.draw.circle(screen, (255, 0, 0), (640, 360), 50)
    pygame.display.flip()
    # --------------------------------------

    # 3. Extract the screen pixels and pipe them to FFmpeg
    raw_frame = pygame.image.tostring(screen, "RGBA")
    ffmpeg_process.stdin.write(raw_frame)

    clock.tick(30)

ffmpeg_process.stdin.close()
ffmpeg_process.wait()
pygame.quit()
