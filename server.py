import os
import sys
import json
import shutil
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory

# PyInstaller로 하나의 실행파일(.exe 등)로 묶었을 때는 sys.frozen이 True가 되고,
# 번들에 포함된 파일은 sys._MEIPASS 임시 폴더에서 풀려 나온다. 반면 실제 쓰기가
# 필요한 데이터(users.json, history.json 등)는 그 임시 폴더가 아니라 exe 옆의
# 실제 폴더에 둬야 다음 실행 때도 내용이 남는다. 일반 `python server.py` 실행
# 때는 두 경로가 같으므로 지금까지와 동일하게 동작한다.
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))

STATIC_DIR = BUNDLE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
SEED_DATA_DIR = BUNDLE_DIR / "data_seed"  # .exe 안에 들어있는 초기 데이터 (최초 실행 시 복사용)
USERS_FILE = DATA_DIR / "users.json"
HISTORY_FILE = DATA_DIR / "history.json"
RAW_LOG_FILE = DATA_DIR / "raw_log.json"


def ensure_data_dir():
    """실행파일(.exe) 더블클릭으로 처음 실행됐을 때, exe 옆에 data 폴더가 없으면
    번들에 들어있던 기본 데이터(테스트 계정 등)로 만들어준다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if FROZEN:
        for name in ("users.json", "history.json"):
            dest = DATA_DIR / name
            src = SEED_DATA_DIR / name
            if not dest.exists() and src.exists():
                shutil.copy(src, dest)


def open_browser_later(port, delay=1.2):
    threading.Timer(delay, lambda: webbrowser.open(f"http://localhost:{port}")).start()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_BASE = "https://api.openai.com/v1"

STATUS_COLOR = {"양호": "#7A9B6E", "주의": "#D9A441", "위험": "#C9603D"}

DEMO_VOICE_STEPS = {
    "식사": {"answer": "네, 아침에 죽 한 그릇 다 먹었어요", "status": "양호", "summary": "식사를 잘 챙기고 계세요"},
    "통증": {"answer": "요즘 무릎이 좀 시큰거리긴 하는데 괜찮아요", "status": "주의", "summary": "무릎 통증이 있으나 일상생활에는 지장 없어 보여요"},
    "수면": {"answer": "잘 자는 편인데 새벽에 한 번씩 깨긴 해요", "status": "양호", "summary": "수면은 대체로 양호한 편이에요"},
}

# "복순이" 반려 AI 페르소나 — 먼저 말 걸기(선제적 대화) 기능에 사용
COMPANION_SYSTEM_PROMPT = """당신은 독거노인의 외로움을 달래주는 귀엽고 따뜻한 반려 AI 로봇 '복순이'입니다.

[대화 규칙]
1. 어르신을 '어르신~' 또는 친근하게 부르며 다정하고 살갑게 다가가세요.
2. 너무 딱딱한 행정 톤은 절대 금물입니다. 손주나 다정한 반려 로봇처럼 이모티콘(🌸, 🧡, 👵)을 섞어 2~3문장 이내로 짧고 쉽게 말하세요.
3. 어르신과의 수다 속에서 자연스럽게 오늘 식사하셨는지, 아픈 곳은 없는지, 잠은 잘 주무셨는지 안부를 물어보세요."""

DEMO_WELCOME_TEXT = "어르신~! 귀염둥이 복순이 왔어요! 🌸 오늘 기분은 어떠세요? 식사는 맛있게 드셨어요? 🧡"

# 데모 모드에서 순서대로 돌아가는 대화 예시 (실제 키가 없거나 호출이 실패할 때)
DEMO_COMPANION_TURNS = [
    {
        "user": "응, 오늘 아침에 밥 반 공기 먹었어",
        "ai": "반 공기라도 챙겨 드셔서 다행이에요! 🌸 저녁엔 조금 더 든든하게 드셔야 해요~ 🧡",
        "health": {"식사": "양호", "통증": "확인불가", "기분": "좋음"},
    },
    {
        "user": "어젯밤에 다리가 좀 저려서 잠을 설쳤어",
        "ai": "아이고 다리가 저리셨구나 😢 너무 오래 앉아 계시지 말고 가볍게 스트레칭 해보세요~ 🌸",
        "health": {"식사": "확인불가", "통증": "통증있음", "기분": "확인불가"},
    },
    {
        "user": "오늘은 그냥저냥 그랬어, 혼자 있으니까 좀 심심하네",
        "ai": "심심하셨겠어요 🧡 복순이가 자주 놀러올게요! 날씨 좋으면 잠깐 마당이라도 나가보시는 거 어때요? 🌸",
        "health": {"식사": "확인불가", "통증": "확인불가", "기분": "외로움"},
    },
]

app = Flask(__name__, static_folder=None)


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_log(kind, data):
    """수집된 원본 데이터를 data/raw_log.json 에 누적 저장한다 (최근 300건 유지)."""
    logs = load_json(RAW_LOG_FILE, [])
    entry = {"timestamp": datetime.now().isoformat(timespec="seconds"), "kind": kind}
    entry.update(data)
    logs.append(entry)
    logs = logs[-300:]
    save_json(RAW_LOG_FILE, logs)


def has_api_key():
    return bool(OPENAI_API_KEY)


def only_digits(s):
    """숫자가 아닌 문자를 모두 제거한다 (공백, 하이픈, 조사 등 STT/GPT가 섞어 넣는 것 방지)."""
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def normalize_name(s):
    return str(s or "").strip().replace(" ", "")


MIN_AUDIO_BYTES = 500  # 이보다 작으면 마이크를 눌렀다 바로 끈 것으로 보고 API 호출 자체를 생략한다


def audio_too_short(file_storage):
    """녹음 파일이 거의 비어있는지 확인한다 (Whisper의 무음 할루시네이션 방지)."""
    try:
        stream = file_storage.stream
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(pos)
        return size < MIN_AUDIO_BYTES
    except Exception:  # noqa: BLE001
        return False


def _openai_error_message(resp):
    """OpenAI 오류 응답에서 사람이 읽을 수 있는 메시지를 뽑아낸다."""
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message")
        if msg:
            return f"HTTP {resp.status_code}: {msg}"
    except Exception:  # noqa: BLE001
        pass
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


def transcribe_audio(file_storage):
    """Whisper STT 호출.

    반환값: (텍스트 또는 None, 'live'|'demo', 실패 사유 또는 None)
    키가 아예 없으면 사유 없이 데모로 돌아가고, 호출은 됐는데 실패하면
    사유 문자열을 함께 돌려준다 (화면/로그에서 원인 확인용).
    """
    if not has_api_key():
        return None, "demo", "OPENAI_API_KEY가 설정되어 있지 않습니다"
    try:
        files = {
            "file": (
                file_storage.filename or "audio.webm",
                file_storage.stream,
                file_storage.mimetype or "audio/webm",
            )
        }
        data = {"model": "whisper-1", "language": "ko"}
        resp = requests.post(
            f"{OPENAI_BASE}/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files=files,
            data=data,
            timeout=30,
        )
        if not resp.ok:
            reason = _openai_error_message(resp)
            app.logger.warning("Whisper 호출 실패, 데모 모드로 대체: %s", reason)
            return None, "demo", reason
        return resp.json().get("text", "").strip(), "live", None
    except Exception as e:  # noqa: BLE001 - 데모 폴백을 위해 광범위하게 처리
        reason = f"{type(e).__name__}: {e}"
        app.logger.warning("Whisper 호출 실패, 데모 모드로 대체: %s", reason)
        return None, "demo", reason


def chat_text(messages, temperature=0.4):
    """GPT 호출 후 원문 텍스트 반환. 실패하면 (None, 'demo', 사유)."""
    if not has_api_key():
        return None, "demo", "OPENAI_API_KEY가 설정되어 있지 않습니다"
    try:
        resp = requests.post(
            f"{OPENAI_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": OPENAI_CHAT_MODEL, "messages": messages, "temperature": temperature},
            timeout=30,
        )
        if not resp.ok:
            reason = _openai_error_message(resp)
            app.logger.warning("GPT 호출 실패, 데모 모드로 대체: %s", reason)
            return None, "demo", reason
        return resp.json()["choices"][0]["message"]["content"].strip(), "live", None
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {e}"
        app.logger.warning("GPT 호출 실패, 데모 모드로 대체: %s", reason)
        return None, "demo", reason


def chat_json(prompt):
    """단일 system 프롬프트로 GPT를 호출해 JSON으로 파싱한다. 실패하면 (None, 'demo', 사유)."""
    raw, mode, reason = chat_text([{"role": "system", "content": prompt}], temperature=0.2)
    if mode != "live" or raw is None:
        return None, "demo", reason
    try:
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned), "live", None
    except Exception as e:  # noqa: BLE001
        reason = f"GPT 응답을 JSON으로 해석하지 못함: {e} (원문: {raw[:150]})"
        app.logger.warning("GPT 응답 JSON 파싱 실패, 데모 모드로 대체: %s", reason)
        return None, "demo", reason


def extract_health(user_text):
    """어르신 발화에서 식사/통증/기분 상태를 JSON으로 추출한다."""
    prompt = (
        "다음 어르신의 대화 내용에서 식사 상태, 신체 통증, 기분/정서 상태를 파악해 JSON으로만 추출하세요.\n"
        f"대화: \"{user_text}\"\n"
        "출력 형식: {\"식사\": \"양호/미흡/확인불가\", \"통증\": \"없음/통증있음/확인불가\", \"기분\": \"좋음/우울/외로움/확인불가\"}"
    )
    result, mode, reason = chat_json(prompt)
    if mode != "live" or not result:
        return {"식사": "확인불가", "통증": "확인불가", "기분": "확인불가"}, "demo", reason
    return result, "live", None


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/login", methods=["POST"])
def api_login():
    audio = request.files.get("audio")
    if audio is None:
        return jsonify({"ok": False, "message": "음성 파일이 없어요"}), 400
    if audio_too_short(audio):
        return jsonify({"ok": False, "too_short": True, "message": "녹음이 너무 짧아요"})

    text, mode, reason = transcribe_audio(audio)
    parsed, cmode, creason = (None, "demo", None)

    if mode == "live":
        prompt = (
            "다음은 어르신이 음성으로 본인 확인을 위해 말한 내용을 받아적은 것입니다: \"" + text + "\"\n"
            "여기서 이름, 주민등록번호 앞 6자리, 뒷자리 첫 번째 숫자를 추출하세요.\n\n"
            "[숫자 변환 규칙]\n"
            "- 어르신이 숫자를 하나씩 끊어 읽은 경우(예: '오공공일공일') 그대로 자릿수만큼 숫자로 바꾸세요:\n"
            "  영/공=0, 일/하나=1, 이/둘=2, 삼/셋=3, 사/넷=4, 오/다섯=5, 육/여섯=6, 칠/일곱=7, 팔/여덟=8, 구/아홉=9\n"
            "- 이미 '오공공일공일'이 아니라 '500101'처럼 숫자로 받아적혀 있으면 그대로 사용하세요.\n"
            "- 결과의 rrn_front, rrn_last1 값에는 숫자만 남기고 공백, 하이픈, 다른 글자는 절대 포함하지 마세요.\n\n"
            "반드시 다음 JSON 형식으로만 답하세요:\n"
            "{\"name\": \"이름\", \"rrn_front\": \"앞6자리 숫자만\", \"rrn_last1\": \"뒷자리첫숫자 1자리\"}\n"
            "추출할 수 없는 항목은 빈 문자열로 두세요."
        )
        parsed, cmode, creason = chat_json(prompt)

    users = load_json(USERS_FILE, [])

    if parsed and cmode == "live":
        p_name = normalize_name(parsed.get("name"))
        p_front = only_digits(parsed.get("rrn_front"))
        p_last1 = only_digits(parsed.get("rrn_last1"))
        match = next(
            (
                u for u in users
                if normalize_name(u["name"]) == p_name
                and u["rrn_front"] == p_front
                and u["rrn_last1"] == p_last1
            ),
            None,
        )
        append_log("login", {
            "recognized_text": text, "matched": bool(match), "mode": "live",
            "extracted_name": parsed.get("name"), "extracted_rrn_front": p_front, "extracted_rrn_last1": p_last1,
        })
        if match:
            return jsonify({"ok": True, "name": match["name"], "recognized_text": text, "mode": "live"})
        return jsonify({
            "ok": False,
            "recognized_text": text,
            "mode": "live",
            "extracted_name": parsed.get("name"),
            "extracted_rrn_front": p_front,
            "extracted_rrn_last1": p_last1,
        })

    # 데모 모드: 키가 없거나 실제 호출이 실패한 경우, 시드된 사용자로 로그인 성공 처리
    debug_reason = reason or creason
    demo_user = users[0] if users else {"name": "김순자"}
    append_log("login", {"recognized_text": text, "matched": True, "mode": "demo", "debug_reason": debug_reason})
    resp = {
        "ok": True,
        "name": demo_user["name"],
        "recognized_text": text or "(데모 모드: 실제 음성 인식 없이 진행)",
        "mode": "demo",
    }
    if debug_reason:
        resp["debug_reason"] = debug_reason
    return jsonify(resp)


@app.route("/api/voice-checkin", methods=["POST"])
def api_voice_checkin():
    topic = request.form.get("topic", "")
    question = request.form.get("question", "")
    audio = request.files.get("audio")
    if audio is None:
        return jsonify({"ok": False, "message": "음성 파일이 없어요"}), 400
    if audio_too_short(audio):
        return jsonify({"ok": False, "too_short": True, "message": "녹음이 너무 짧아요"})

    text, mode, reason = transcribe_audio(audio)
    analysis, cmode, creason = (None, "demo", None)

    if mode == "live":
        prompt = (
            f"어르신의 대답: \"{text}\"\n"
            f"점검 항목: \"{topic}\" (질문: {question})\n\n"
            "이 대답을 바탕으로 어르신의 상태를 [양호, 주의, 위험] 중 하나로 평가하고 "
            "한국어로 한 문장 요약하세요.\n"
            "반드시 다음 JSON 형식으로만 답변하세요:\n"
            "{\"상태\": \"양호\", \"요약\": \"한 문장 요약\"}"
        )
        analysis, cmode, creason = chat_json(prompt)

    if analysis and cmode == "live" and analysis.get("상태") in STATUS_COLOR:
        status = analysis["상태"]
        append_log("voice_checkin", {
            "topic": topic, "question": question, "recognized_text": text,
            "status": status, "summary": analysis.get("요약", ""), "mode": "live",
        })
        return jsonify(
            {
                "ok": True,
                "answer": text,
                "status": status,
                "summary": analysis.get("요약", ""),
                "color": STATUS_COLOR[status],
                "mode": "live",
            }
        )

    debug_reason = reason or creason
    fallback = DEMO_VOICE_STEPS.get(topic, {"answer": text or "", "status": "양호", "summary": "확인했어요"})
    status = fallback["status"]
    append_log("voice_checkin", {
        "topic": topic, "question": question, "recognized_text": text or fallback["answer"],
        "status": status, "summary": fallback["summary"], "mode": "demo", "debug_reason": debug_reason,
    })
    resp = {
        "ok": True,
        "answer": text or fallback["answer"],
        "status": status,
        "summary": fallback["summary"],
        "color": STATUS_COLOR[status],
        "mode": "demo",
    }
    if debug_reason:
        resp["debug_reason"] = debug_reason
    return jsonify(resp)


@app.route("/api/checkin/complete", methods=["POST"])
def api_checkin_complete():
    payload = request.get_json(force=True, silent=True) or {}
    results = payload.get("results", [])
    order = {"위험": 0, "주의": 1, "양호": 2}
    overall_status = "양호"
    if results:
        overall_status = min(results, key=lambda r: order.get(r.get("status"), 3)).get("status", "양호")

    history = load_json(HISTORY_FILE, [])
    history = [h for h in history if h.get("label") != "오늘"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    history.insert(
        0,
        {
            "date": today_str,
            "label": "오늘",
            "status": overall_status,
            "color": STATUS_COLOR.get(overall_status, "#7A9B6E"),
            "results": results,
            "sample": False,
        },
    )
    save_json(HISTORY_FILE, history)
    return jsonify({"ok": True})


@app.route("/api/history", methods=["GET"])
def api_history():
    return jsonify({"history": load_json(HISTORY_FILE, [])})


# ---------------------------------------------------------------------
# "먼저 말 걸기" (복순이 선제적 대화) — AI가 먼저 인사하고 자유롭게 대화하며
# 대화 속에서 식사/통증/기분 상태를 자동으로 추출한다.
# ---------------------------------------------------------------------

@app.route("/api/companion/start", methods=["GET"])
def api_companion_start():
    text, mode, reason = chat_text(
        [
            {"role": "system", "content": COMPANION_SYSTEM_PROMPT},
            {"role": "user", "content": "어르신께 자연스럽게 첫 인사를 건네고 안부를 물어보세요. 짧고 다정하게, 이모지를 섞어서 2~3문장으로 말해주세요."},
        ],
        temperature=0.7,
    )
    if mode == "live" and text:
        return jsonify({"ok": True, "ai_reply_text": text, "mode": "live"})

    resp = {"ok": True, "ai_reply_text": DEMO_WELCOME_TEXT, "mode": "demo"}
    if reason:
        resp["debug_reason"] = reason
    return jsonify(resp)


@app.route("/api/companion/chat", methods=["POST"])
def api_companion_chat():
    audio = request.files.get("audio")
    if audio is None:
        return jsonify({"ok": False, "message": "음성 파일이 없어요"}), 400
    if audio_too_short(audio):
        return jsonify({"ok": False, "too_short": True, "message": "녹음이 너무 짧아요"})

    try:
        history_list = json.loads(request.form.get("chat_history", "[]"))
        if not isinstance(history_list, list):
            history_list = []
    except Exception:  # noqa: BLE001
        history_list = []

    turn_index = sum(1 for h in history_list if h.get("role") == "user")

    text, mode, reason = transcribe_audio(audio)
    creason = None

    if mode == "live":
        messages = [{"role": "system", "content": COMPANION_SYSTEM_PROMPT}]
        messages.extend(history_list[-6:])
        messages.append({"role": "user", "content": text})

        ai_reply, cmode, creason = chat_text(messages, temperature=0.7)
        health, hmode, hreason = extract_health(text)

        if cmode == "live" and ai_reply:
            append_log("companion", {
                "user_text": text, "ai_reply": ai_reply, "health_analysis": health, "mode": "live",
            })
            return jsonify(
                {
                    "ok": True,
                    "user_recognized_text": text,
                    "ai_reply_text": ai_reply,
                    "health_analysis": health,
                    "mode": "live",
                }
            )

    # 데모 모드: 키가 없거나 호출이 실패한 경우, 미리 준비된 대화를 순서대로 보여준다
    debug_reason = reason or creason
    demo_turn = DEMO_COMPANION_TURNS[turn_index % len(DEMO_COMPANION_TURNS)]
    append_log("companion", {
        "user_text": text or demo_turn["user"], "ai_reply": demo_turn["ai"],
        "health_analysis": demo_turn["health"], "mode": "demo", "debug_reason": debug_reason,
    })
    resp = {
        "ok": True,
        "user_recognized_text": text or demo_turn["user"],
        "ai_reply_text": demo_turn["ai"],
        "health_analysis": demo_turn["health"],
        "mode": "demo",
    }
    if debug_reason:
        resp["debug_reason"] = debug_reason
    return jsonify(resp)


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """수집된 원본 입력/응답 로그를 최신순으로 반환한다 (데이터 확인용)."""
    logs = load_json(RAW_LOG_FILE, [])
    return jsonify({"logs": list(reversed(logs))})


if __name__ == "__main__":
    ensure_data_dir()
    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    if FROZEN:
        print("독거노인 건강 체크인 앱 (실행파일 모드)")
    print("OPENAI_API_KEY:", "설정됨 (실제 STT/AI 분석 사용)" if has_api_key() else "없음 (데모 모드로 동작)")
    print(f"http://localhost:{port} 에서 접속하세요")
    print("=" * 60)
    if FROZEN:
        # 더블클릭으로 실행했을 때는 브라우저를 자동으로 열어준다.
        # Flask의 디버그 리로더는 실행파일 자체를 재실행하려 들어 문제가 생기므로 끈다.
        open_browser_later(port)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    else:
        app.run(host="0.0.0.0", port=port, debug=True)
