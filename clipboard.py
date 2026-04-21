import os
import subprocess
from flask import Flask, Response, request

app = Flask(__name__)

SCREEN_W = int(os.environ.get("SCREEN_W", "1080"))
SCREEN_H = int(os.environ.get("SCREEN_H", "2400"))


@app.route("/clipboard")
def clipboard():
    result = subprocess.run(
        ["xclip", "-o", "-selection", "clipboard"],
        capture_output=True,
        text=True,
        env={"DISPLAY": ":1"},
    )
    content = result.stdout if result.returncode == 0 else ""
    return Response(content, mimetype="text/plain; charset=utf-8")


@app.route("/swipe")
def swipe():
    direction = request.args.get("dir", "up")
    amount = int(request.args.get("amount", "400"))
    amount = max(100, min(amount, 1600))
    cx = int(request.args.get("x", SCREEN_W // 2))
    cy = int(request.args.get("y", SCREEN_H // 2))
    cx = max(0, min(cx, SCREEN_W))
    cy = max(0, min(cy, SCREEN_H))
    if direction == "up":
        x1, y1 = cx, cy
        x2, y2 = cx, max(0, cy - amount)
    else:
        x1, y1 = cx, cy
        x2, y2 = cx, min(SCREEN_H, cy + amount)
    duration = request.args.get("duration", "180")
    subprocess.run(
        ["adb", "shell", "input", "swipe",
         str(x1), str(y1), str(x2), str(y2), duration],
        check=False,
    )
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6081)
