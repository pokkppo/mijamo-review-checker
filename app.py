# app.py
# 미자모 서평 검토기 - Flask 백엔드
# 2026-08-15 v2.1 - Render 배포 대응 (PORT 환경변수, 0.0.0.0 바인딩)

from flask import Flask, request, jsonify, send_from_directory
from checker import run_review
import os

app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/review", methods=["POST"])
def api_review():
    data = request.get_json(force=True)
    raw_text = data.get("table_text", "")
    if not raw_text.strip():
        return jsonify({"error": "입력된 데이터가 없습니다."}), 400
    try:
        results = run_review(raw_text)
    except Exception as e:
        return jsonify({"error": f"검토 중 오류가 발생했습니다: {e}"}), 500
    return jsonify({"results": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
