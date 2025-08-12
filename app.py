from flask import Flask, render_template, request
import itertools, random, urllib.parse, requests, datetime, time, uuid, sys
from collections import defaultdict
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os, json, hmac, hashlib
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
app = Flask(__name__)

# --------------------------------
# 기본 설정
# --------------------------------
positions = ["탑", "정글", "미드", "원딜", "서폿"]
RELAY_BASE = 'https://script.google.com/macros/s/AKfycbzBjy0DKvUHiw3iFaJJicEkLboO3DnGNdUeyTUiqfHUa14eY5Vjy0xnRVYlU30-lstlNg/exec'
RELAY_KEY  = 'hawawasiegetan'  # GAS SHARED_KEY와 동일
# 디스코드 웹훅
DISCORD_WEBHOOK_URL = 'https://discord.com/api/webhooks/1404896679328616548/nkJjDmOipmZRzifqmAtPL4nsPsNdNREbrEUiRA3iYl_KeqfqzdQQpKovZgFtwdanVD4Q'
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "").strip()
# 구글 시트
SHEET_NAME = "hawawa_db"                                # 스프레드시트(문서) 이름
CREDS_FILE = "hawawa-teambuilder-e8bc633550b7.json"     # 서비스 계정 JSON
SCORES_WS = "기록장"                                     # 점수 있는 탭
PENDING_WS = "pending"                                   # 미기록 조합 관리 탭

# 배포 URL
BASE_URL = "https://seigetan.pythonanywhere.com"

# 메모리 저장(서버 재시작시 리셋 OK)
POLLS = {}  # {pid: {"title":str, "options":[...], "option_links":[...], "votes":{nick:idx}, "closed":bool, "created_at":int}}

# 디스코드 전송 스로틀(초당 1건)
LAST_SEND_TS = 0.0
MIN_INTERVAL = 1.0  # 초당 1건

def verify_discord_signature(req):
    """Discord Interactions 서명 검증 (Ed25519)"""
    if not DISCORD_PUBLIC_KEY:
        return False
    sig = request.headers.get("X-Signature-Ed25519", "")
    ts  = request.headers.get("X-Signature-Timestamp", "")
    body = request.get_data(as_text=False)  # bytes
    try:
        vk = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        vk.verify(ts.encode() + body, bytes.fromhex(sig))
        return True
    except Exception:
        return False
# --------------------------------
# Google Sheets 헬퍼
# --------------------------------
def gs_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    return gspread.authorize(creds)

def get_results_ws(client):
    return client.open(SHEET_NAME).sheet1

def get_or_create_pending_ws(client):
    ss = client.open(SHEET_NAME)
    try:
        ws = ss.worksheet(PENDING_WS)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=PENDING_WS, rows=1000, cols=3)
        ws.update('A1:C1', [['timestamp', 'link', 'status']])
    return ws

def pending_add(link):
    try:
        client = gs_client()
        ws = get_or_create_pending_ws(client)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([ts, link, "No"], value_input_option='USER_ENTERED')
    except Exception as e:
        print("⚠️ pending_add 실패:", e, file=sys.stderr)

def pending_mark_done_by_link(link):
    try:
        client = gs_client()
        ws = get_or_create_pending_ws(client)
        rows = ws.get_all_values()
        for i in range(2, len(rows)+1):   # 1행은 헤더
            row = rows[i-1]
            row_link = row[1] if len(row) > 1 else ""
            status   = row[2] if len(row) > 2 else ""
            if row_link == link and status != "Done":
                ws.update_cell(i, 3, "Done")
                return True
    except Exception as e:
        print("⚠️ pending_mark_done_by_link 실패:", e, file=sys.stderr)
    return False

def pending_fetch_unrecorded():
    links = []
    try:
        client = gs_client()
        ws = get_or_create_pending_ws(client)
        rows = ws.get_all_values()
        for r in rows[1:]:
            if len(r) >= 3 and r[2] != "Done" and r[1]:
                links.append(r[1])
    except Exception as e:
        print("⚠️ pending_fetch_unrecorded 실패:", e, file=sys.stderr)
    return links

def load_scores_map():
    """
    기록장 탭을 읽어 이름 -> [탑,정글,미드,원딜,서폿] 매핑 반환
    """
    try:
        client = gs_client()
        ws = client.open(SHEET_NAME).worksheet(SCORES_WS)
        rows = ws.get_all_values()
    except Exception as e:
        print("⚠️ load_scores_map 실패:", e, file=sys.stderr)
        return {}

    score_map = {}
    if not rows:
        return score_map

    for r in rows[1:]:  # 1행 헤더 가정
        if len(r) < 6:
            continue
        name = (r[0] or "").strip()
        if not name:
            continue
        cols = r[1:6]
        scores = []
        for v in cols:
            v = (v or "").strip()
            if v.lstrip("-").isdigit():
                scores.append(int(v))
            else:
                try:
                    scores.append(int(float(v)))
                except:
                    scores.append(0)
        score_map[name] = scores
    return score_map


# --------------------------------
# 입력 파서 (이름만 또는 점수 포함 혼합 입력)
# --------------------------------
def parse_input_mixed(text, score_lookup):
    """
    - '이름\\t탑\\t정글\\t미드\\t원딜\\t서폿' 형식이면 그대로 사용
    - '이름'만 있으면 score_lookup에서 점수 채움
    - 점수 못 찾으면 (name, None) 으로 표시
    """
    players = []
    for raw in text.strip().split("\n"):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("\t") if p.strip() != ""]
        if len(parts) == 1:
            name = parts[0]
            scores = score_lookup.get(name)
            if scores and len(scores) == 5:
                players.append((name, scores))
            else:
                players.append((name, None))
        elif len(parts) == 6:
            name = parts[0]
            try:
                scores = list(map(int, parts[1:]))
                players.append((name, scores))
            except:
                pass
        else:
            # 무시
            pass
    return players


# --------------------------------
# 팀 배치/조합 계산
# --------------------------------
def valid_assignments(team, king_name=None, king_position=None):
    results = []
    king_index = None
    if king_name and king_position:
        for i, (name, _) in enumerate(team):
            if name == king_name:
                king_index = i
                break
    if king_name and king_index is None:
        return []

    if king_name:
        king_pos_index = positions.index(king_position)
        player_indices = list(range(5))
        pos_indices = list(range(5))
        player_indices.remove(king_index)
        pos_indices.remove(king_pos_index)

        for perm in itertools.permutations(pos_indices):
            total_score = 0
            assignment = []
            valid = True
            king_score = team[king_index][1][king_pos_index]
            if king_score <= 0:
                continue
            assignment.append((positions[king_pos_index], team[king_index][0], king_score))
            total_score += king_score
            for p_idx, pos in zip(player_indices, perm):
                name, scores = team[p_idx]
                score = scores[pos]
                if score <= 0:
                    valid = False
                    break
                assignment.append((positions[pos], name, score))
                total_score += score
            if valid:
                assignment = sorted(assignment, key=lambda x: positions.index(x[0]))
                results.append((total_score, assignment))
    else:
        for perm in itertools.permutations(range(5)):
            total_score = 0
            assignment = []
            valid = True
            for player, pos in zip(team, perm):
                name, scores = player
                score = scores[pos]
                if score <= 0:
                    valid = False
                    break
                assignment.append((positions[pos], name, score))
                total_score += score
            if valid:
                assignment = sorted(assignment, key=lambda x: positions.index(x[0]))
                results.append((total_score, assignment))
    return results


# --------------------------------
# 투표 유틸 (pid만 사용)
# --------------------------------
def _make_poll(title, options, option_links):
    pid = uuid.uuid4().hex
    POLLS[pid] = {
        "title": title,
        "options": list(options),
        "option_links": list(option_links),
        "votes": {},
        "closed": False,
        "created_at": int(time.time())
    }
    return pid

def make_poll_links(title, options, option_links):
    pid = _make_poll(title, options, option_links)
    vote_link = f"{BASE_URL}/vote?pid={pid}"
    end_link  = f"{BASE_URL}/vote/end?pid={pid}"
    return vote_link, end_link


# --------------------------------
# 안전한 디스코드 전송 (초당 1건 + 자세한 로그)
# --------------------------------
def send_to_discord(content, username=None, max_retry=0):
    """웹훅 전송: GAS 릴레이로 전송 (초당 1건 스로틀 + 진단 로그 유지)"""
    import time, sys, requests
    global LAST_SEND_TS
    now = time.time()
    wait = MIN_INTERVAL - (now - LAST_SEND_TS)
    if wait > 0:
        time.sleep(wait)

    payload = {"content": content}
    url = f"{RELAY_BASE}?key={RELAY_KEY}"

    try:
        resp = requests.post(
            url, json=payload, timeout=12,
            headers={"Content-Type": "application/json"}
        )
    except Exception as e:
        print(f"[RELAY] 요청 예외: {repr(e)}", file=sys.stderr)
        return False

    print(f"[RELAY] status={resp.status_code} body[:200]={resp.text[:200]!r}", file=sys.stderr)

    if 200 <= resp.status_code < 300 and resp.text.startswith("ok"):
        LAST_SEND_TS = time.time()
        return True
    return False


def send_long_to_discord(text, username=None, chunk_size=1800):
    """메시지가 너무 길면 분할(각 chunk마다 초당 1건 스로틀 적용)"""
    print(f"[WEBHOOK] sending text length={len(text)}", file=sys.stderr)
    if len(text) <= chunk_size:
        send_to_discord(text, username=username)
        return
    i = 0
    while i < len(text):
        chunk = text[i:i+chunk_size]
        send_to_discord(chunk, username=username)
        i += chunk_size


# --------------------------------
# Discord 전송(랜덤 조합: 메시지 하나로 합쳐 전송, 코드 링크는 본문 표시 X)
# --------------------------------
def send_to_discord_with_code(matches, title, raw_input):
    encoded = urllib.parse.quote(raw_input)
    all_messages = []
    option_links = []  # 종료 시 공개/사용

    for idx, (score_a, team_a, score_b, team_b) in enumerate(matches, 1):
        # 조합코드 링크(본문에는 표시 X, 종료 시 사용)
        code_link = (
            f"{BASE_URL}/조합코드"
            f"?input={encoded}"
            f"&a={','.join([p[1] for p in team_a])}"
            f"&b={','.join([p[1] for p in team_b])}"
        )
        option_links.append(code_link)

        lines = [
            f"**{title} 조합 {idx}**",
            f"총합 A: {score_a} / 총합 B: {score_b}",
            "```",
            f"{'Team A':<25} | {'Team B':<25}",
            "-" * 53
        ]
        for i in range(5):
            pos_a, name_a, s_a = team_a[i]
            pos_b, name_b, s_b = team_b[i]
            left = f"{pos_a}: {name_a} ({s_a})"
            right = f"{pos_b}: {name_b} ({s_b})"
            lines.append(f"{left:<23} | {right:<23}")  # 간격 약간 좁힘
        lines.append("```")
        all_messages.append("\n".join(lines))

    # ✅ 조합 3개 → 한 덩어리 텍스트로 1회 전송
    combined = "\n\n".join(all_messages)
    send_long_to_discord(combined)

    # ✅ 투표/종료 링크도 1회만
    labels = [f"{i}번" for i in range(1, len(option_links)+1)]
    poll_title = f"{title} 전체 투표"
    vote_link, end_link = make_poll_links(poll_title, labels, option_links)
    final_links = f"🗳️ 투표: {vote_link}\n⏹️ 종료: {end_link}"
    send_to_discord(final_links)


# --------------------------------
# 라우트: 메인
# --------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    default_input = """김희철
두낫
밈밈
소라다요
오곡
용
재재재
쿨라떼
찬욱
알리꼬"""
    if request.method == "POST":
        input_text = request.form.get("player_data", default_input)
        king_name = request.form.get("king_name")
        king_position = request.form.get("king_position")
        action = request.form.get("action")
        page = int(request.form.get("page", 1))
        if king_name == "(왕 없음)":
            king_name = None
            king_position = None

        # 이름만 입력 허용: 시트 점수 매핑 로드 → 혼합 파싱
        scores_map = load_scores_map()
        players_raw = parse_input_mixed(input_text, scores_map)

        # 점수 못찾은 이름 에러
        missing = [name for (name, sc) in players_raw if sc is None]
        if missing:
            return render_template(
                "index.html",
                error=f"⚠️ 시트 '{SCORES_WS}'에서 점수를 찾지 못한 이름: {', '.join(missing)}",
                default_input=input_text,
                names=[p[0] for p in players_raw if p[1] is not None],
                positions=positions
            )

        players = [(name, sc) for (name, sc) in players_raw if sc is not None]
        if len(players) != 10:
            return render_template("index.html", error="⚠️ 정확히 10명의 플레이어를 입력해주세요.", default_input=input_text)

        exact_matches, five_point_matches = [], []
        for team_a_indices in itertools.combinations(range(10), 5):
            team_b_indices = [i for i in range(10) if i not in team_a_indices]
            team_a = [players[i] for i in team_a_indices]
            team_b = [players[i] for i in team_b_indices]
            if king_name:
                if king_name in [p[0] for p in team_a]:
                    team_a_assigns = valid_assignments(team_a, king_name, king_position)
                    team_b_assigns = valid_assignments(team_b)
                elif king_name in [p[0] for p in team_b]:
                    team_a_assigns = valid_assignments(team_a)
                    team_b_assigns = valid_assignments(team_b, king_name, king_position)
                else:
                    continue
            else:
                team_a_assigns = valid_assignments(team_a)
                team_b_assigns = valid_assignments(team_b)

            for score_a, assign_a in team_a_assigns:
                for score_b, assign_b in team_b_assigns:
                    diff = abs(score_a - score_b)
                    if diff == 0:
                        exact_matches.append((score_a, assign_a, score_b, assign_b))
                    elif diff == 5:
                        five_point_matches.append((score_a, assign_a, score_b, assign_b))

        if action == "random_exact":
            matches = random.sample(exact_matches, min(3, len(exact_matches)))
            send_to_discord_with_code(matches, "0점 차이", input_text)
            return render_template("index.html", result_type="random", matches=matches,
                                   default_input=input_text, names=[p[0] for p in players], positions=positions)
        elif action == "random_five":
            matches = random.sample(five_point_matches, min(3, len(five_point_matches)))
            send_to_discord_with_code(matches, "5점 차이", input_text)
            return render_template("index.html", result_type="random", matches=matches,
                                   default_input=input_text, names=[p[0] for p in players], positions=positions)
        elif action == "random_all":
            all_matches = exact_matches + five_point_matches
            matches = random.sample(all_matches, min(3, len(all_matches)))
            send_to_discord_with_code(matches, "전체 조합", input_text)
            return render_template("index.html", result_type="random", matches=matches,
                                   default_input=input_text, names=[p[0] for p in players], positions=positions)
        else:
            per_page = 10
            start = (page - 1) * per_page
            return render_template(
                "index.html",
                result_type="full",
                exact_matches=exact_matches[start:start+per_page],
                five_matches=five_point_matches[start:start+per_page],
                exact_total=len(exact_matches),
                five_total=len(five_point_matches),
                current_page=page,
                per_page=per_page,
                default_input=input_text,
                names=[p[0] for p in players],
                positions=positions,
                king_name=king_name or "(왕 없음)",
                king_position=king_position or ""
            )

    # GET
    names = [line.split('\t')[0] for line in default_input.strip().split('\n')]
    return render_template("index.html", default_input=default_input, names=names, positions=positions)


# --------------------------------
# 라우트: 조합코드 (URL 순서대로 고정 표기)
# --------------------------------
@app.route("/조합코드")
def combination_code():
    raw_input = request.args.get("input", "")
    a_csv = request.args.get("a", "")
    b_csv = request.args.get("b", "")
    if not raw_input or not a_csv or not b_csv:
        return render_template("index.html", error="⚠️ 정보가 부족합니다.", default_input=urllib.parse.unquote(raw_input))

    input_text = urllib.parse.unquote(raw_input).strip()
    lines = [l.strip() for l in input_text.split("\n") if l.strip()]

    name_to_scores = {}

    # 1) 점수 포함 형식 시도
    parsed = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 6:
            try:
                parsed.append((parts[0].strip(), list(map(int, parts[1:]))))
            except:
                pass

    if len(parsed) == 10:
        name_to_scores = {n: s for n, s in parsed}
    else:
        # 2) 이름만인 경우, 시트에서 점수 로드
        scores_map = load_scores_map()
        missing = [n for n in lines if n not in scores_map]
        if missing:
            return render_template("index.html",
                                   error=f"⚠️ 시트에서 점수를 찾지 못한 이름: {', '.join(missing)}",
                                   default_input=input_text)
        name_to_scores = {n: scores_map[n] for n in lines}

    def build_team(csv_names):
        names = [n.strip() for n in csv_names.split(",")]
        if len(names) != 5:
            return None
        team = []
        for i, name in enumerate(names):
            scores = name_to_scores.get(name)
            if not scores or len(scores) < 5:
                return None
            team.append((positions[i], name, scores[i]))  # URL 순서: 탑~서폿
        return team

    team_a = build_team(a_csv)
    team_b = build_team(b_csv)
    if not team_a or not team_b:
        return render_template("index.html", error="⚠️ 팀 구성 실패(이름/인원/순서 확인)", default_input=input_text)

    score_a = sum(x[2] for x in team_a)
    score_b = sum(x[2] for x in team_b)

    return render_template("index.html",
                           default_input=input_text,
                           result_type="code",
                           code_match=(score_a, team_a, score_b, team_b),
                           code_value=raw_input,
                           names=list(name_to_scores.keys()),
                           positions=positions)


# --------------------------------
# 라우트: 결과 전송(구글 시트)
#  - 저장 성공 시 해당 조합을 pending에서 Done 처리
# --------------------------------
@app.route("/전송", methods=["POST"])
def submit_result():
    input_data = request.form.get("code_value")   # 인코딩된 원본
    a_team = request.form.get("a_team", "").split(",")
    b_team = request.form.get("b_team", "").split(",")
    winner1 = request.form.get("r1_result")
    winner2 = request.form.get("r2_result")

    if not input_data or not a_team or not b_team or not winner1:
        return "⚠️ 저장 실패: 정보 부족"

    now = datetime.datetime.now()
    date_str = f"{now.strftime('%y')}/{now.month}/{now.day}"
    round1 = "1RD"

    rows_to_append = []

    # 1R
    if winner1 == "A":
        rows_to_append.append([date_str, round1] + a_team + b_team)
    else:
        rows_to_append.append([date_str, round1] + b_team + a_team)

    # 2R (선택)
    if winner2 in ["A", "B"]:
        round2 = "2RD"
        if winner2 == "A":
            rows_to_append.append([date_str, round2] + a_team + b_team)
        else:
            rows_to_append.append([date_str, round2] + b_team + a_team)

    try:
        client = gs_client()
        sheet = get_results_ws(client)
        for row in rows_to_append:
            sheet.append_row(row, value_input_option='USER_ENTERED')

        # 저장 성공했으면 해당 조합 링크를 Done 처리
        combo_link = (
            f"{BASE_URL}/조합코드"
            f"?input={input_data}"
            f"&a={','.join(a_team)}"
            f"&b={','.join(b_team)}"
        )
        pending_mark_done_by_link(combo_link)

        return "✅ 저장 완료"
    except Exception as e:
        return f"⚠️ 저장 실패: {e}"


# --------------------------------
# 라우트: 투표
#  - 종료 시 이전 미기록 조합 경고 + 이번 선택 조합을 pending(No)로 추가
#  - 동점이면 동점 후보 중 랜덤 선정
# --------------------------------
@app.route("/vote", methods=["GET", "POST"])
def vote_page():
    pid = request.args.get("pid", "")
    if not pid or pid not in POLLS:
        return "⚠️ 잘못된 링크입니다.", 400

    poll = POLLS[pid]
    if poll.get("closed"):
        return "⛔ 이미 종료된 투표입니다."

    if request.method == "POST":
        voter = (request.form.get("voter") or "").strip()
        choice = request.form.get("choice")
        if not voter or choice is None or not choice.isdigit():
            return "⚠️ 닉네임과 선택지를 올바르게 입력하세요.", 400
        idx = int(choice)
        if idx < 0 or idx >= len(poll["options"]):
            return "⚠️ 유효하지 않은 선택지입니다.", 400
        poll["votes"][voter] = idx
        return f"✅ {voter} 님의 투표가 저장되었습니다. (다시 투표하면 최신 표로 갱신됩니다)"

    # GET: 간단 폼
    opts = poll["options"]
    radio_html = []
    for i, label in enumerate(opts):
        required = 'required' if i == 0 else ''
        radio_html.append(f'<label><input type="radio" name="choice" value="{i}" {required}> {label}</label>')
    radios = "<br>\n".join(radio_html)

    return f"""
    <h2>🗳️ 투표 — {poll['title']}</h2>
    <form method="POST">
      닉네임: <input name="voter" required>
      <div style="margin: 10px 0;">
        {radios}
      </div>
      <button type="submit">투표하기</button>
    </form>
    """


@app.route("/vote/end", methods=["GET"])
def vote_end_page():
    pid = request.args.get("pid", "")
    if not pid or pid not in POLLS:
        return "⚠️ 잘못된 링크입니다.", 400

    poll = POLLS[pid]
    if poll.get("closed"):
        return "⛔ 이미 종료된 투표입니다."

    return f"""
    <h2>⏹️ 투표 종료 — {poll['title']}</h2>
    <p>정말 종료하시겠습니까? 종료 시 결과가 디스코드로 공지됩니다.</p>
    <form method="POST" action="/vote/end/confirm">
      <input type="hidden" name="pid" value="{pid}">
      <button type="submit">정말 종료</button>
    </form>
    """


@app.route("/vote/end/confirm", methods=["POST"])
def vote_end_confirm():
    pid = request.form.get("pid", "")
    if not pid or pid not in POLLS:
        return "⚠️ 잘못된 요청입니다.", 400

    poll = POLLS[pid]
    if poll.get("closed"):
        return "⛔ 이미 종료된 투표입니다."

    # 이전 미기록 조합 링크 가져오기
    unresolved = pending_fetch_unrecorded()

    # 집계
    counts = defaultdict(int)
    for _, idx in poll["votes"].items():
        if idx < len(poll["options"]):
            counts[idx] += 1
    total_opts = len(poll["options"])
    counts_list = [counts[i] for i in range(total_opts)]

    if not counts_list:
        result_title = f"**투표 종료: {poll['title']}**\n(표가 없습니다)"
        result_link = None
        winner_idx = None
    else:
        max_votes = max(counts_list)
        top_indices = [i for i, c in enumerate(counts_list) if c == max_votes]
        winner_idx = random.choice(top_indices) if len(top_indices) > 1 else top_indices[0]
        tie_note = " (동점 → 랜덤 선정)" if len(top_indices) > 1 else ""
        result_title = f"**투표 종료: {poll['title']}**{tie_note}"
        result_link = poll["option_links"][winner_idx] if winner_idx is not None and winner_idx < len(poll["option_links"]) else None

    # 결과 메시지 구성
    lines = [result_title, "```"]
    for i, label in enumerate(poll["options"]):
        lines.append(f"{label} : {counts_list[i]}")
    lines.append("```")

    # 미기록 조합 경고
    if unresolved:
        lines.append("⚠️ 이전에 승패가 기록되지 않은 조합이 있습니다:")
        for link in unresolved:
            lines.append(f"- {link}")

    # 선택된 조합 안내(링크) 및 pending 등록
    if result_link:
        lines.append(f"✅ 선택된 조합: {result_link}")
        pending_add(result_link)

    try:
        send_long_to_discord("\n".join(lines))
    except Exception as e:
        return f"⚠️ 디스코드 전송 실패: {e}", 500

    poll["closed"] = True
    return "✅ 결과를 디스코드로 공지했습니다. (이 페이지는 닫아도 됩니다)"


# --------------------------------
# 라우트: 미기록 조합 관리(간단)
# --------------------------------
@app.route("/pending", methods=["GET"])
def pending_list():
    links = pending_fetch_unrecorded()
    if not links:
        return "<h3>미기록 조합 없음</h3>"
    items = "".join([f"<li>{urllib.parse.quote(link, safe=':/?=&,')}"
                     f" <form style='display:inline' method='POST' action='/pending/resolve'>"
                     f"<input type='hidden' name='link' value='{link}'>"
                     f"<button type='submit'>해결</button></form></li>"
                     for link in links])
    return f"<h3>미기록 조합 목록</h3><ul>{items}</ul>"

@app.route("/pending/resolve", methods=["POST"])
def pending_resolve():
    link = request.form.get("link", "")
    if not link:
        return "⚠️ 링크 누락", 400
    ok = pending_mark_done_by_link(link)
    return "✅ 해결 처리" if ok else "⚠️ 처리 실패 또는 이미 해결됨"


# --------------------------------
# 라우트: 웹훅 단일 테스트 (진단용)
# --------------------------------
@app.route("/webhook_test")
def webhook_test():
    txt = request.args.get("msg", "테스트 메시지 입니다.")
    ok = send_to_discord(f"[테스트] {txt}")
    return ("✅ 전송 성공" if ok else "❌ 전송 실패, error log 확인"), (200 if ok else 500)

@app.route("/discord", methods=["POST"])
def discord_interactions():
    # 1) 서명 검증 실패 시 401
    if not verify_discord_signature(request):
        return ("invalid request signature", 401)

    payload = request.get_json(force=True, silent=True) or {}

    # 2) PING (type 1) 은 PONG (type 1) 리턴
    if payload.get("type") == 1:
        return {"type": 1}

    # 3) Application Command (type 2)
    if payload.get("type") == 2:
        data = payload.get("data", {})
        cmd_name = (data.get("name") or "").strip()

        # /내전 (하위에 '와' 서브커맨드가 있든 문자열 옵션이 있든 일단 문자열 옵션 'members'로 받도록 구성)
        # 디스코드 포털에서:
        #  - Command: 내전
        #  - String option: members (설명: 멤버 목록, 줄바꿈 또는 탭 구분)
        #  - (선택) String option: mode (exact/five/all) 기본 all
        opts = {o.get("name"): o.get("value") for o in (data.get("options") or [])}
        members_text = (opts.get("members") or "").strip()
        mode = (opts.get("mode") or "all").lower()

        if not members_text:
            # 64 플래그 = ephemeral (요청자에게만 보이기)
            return {
                "type": 4,
                "data": {"content": "⚠️ 멤버 목록을 입력하세요.", "flags": 64}
            }

        # 4) 기존 로직 재사용: 점수 로드 -> 혼합 파싱 -> 전체/0점/5점 풀에서 3개 샘플
        try:
            scores_map = load_scores_map()
            players_raw = parse_input_mixed(members_text, scores_map)
            missing = [name for (name, sc) in players_raw if sc is None]
            if missing:
                return {
                    "type": 4,
                    "data": {"content": f"⚠️ 시트 '{SCORES_WS}'에서 점수를 찾지 못한 이름: {', '.join(missing)}", "flags": 64}
                }

            players = [(n, sc) for (n, sc) in players_raw if sc is not None]
            if len(players) != 10:
                return {"type": 4, "data": {"content": "⚠️ 정확히 10명의 멤버를 입력하세요.", "flags": 64}}

            # 조합 풀 생성 (king 옵션은 Slash에 안 받도록 단순화)
            exact_matches, five_point_matches = [], []
            for team_a_indices in itertools.combinations(range(10), 5):
                team_b_indices = [i for i in range(10) if i not in team_a_indices]
                team_a = [players[i] for i in team_a_indices]
                team_b = [players[i] for i in team_b_indices]
                team_a_assigns = valid_assignments(team_a)
                team_b_assigns = valid_assignments(team_b)
                for score_a, assign_a in team_a_assigns:
                    for score_b, assign_b in team_b_assigns:
                        diff = abs(score_a - score_b)
                        if diff == 0:
                            exact_matches.append((score_a, assign_a, score_b, assign_b))
                        elif diff == 5:
                            five_point_matches.append((score_a, assign_a, score_b, assign_b))

            if mode == "exact":
                pool, title = exact_matches, "0점 차이"
            elif mode == "five":
                pool, title = five_point_matches, "5점 차이"
            else:
                pool, title = (exact_matches + five_point_matches), "전체 조합"

            if not pool:
                return {"type": 4, "data": {"content": "조건을 만족하는 조합이 없습니다.", "flags": 64}}

            picks = random.sample(pool, min(3, len(pool)))

            # 5) 결과는 GAS 릴레이를 통해 채널로 전송 (기존 함수 재사용)
            send_to_discord_with_code(picks, title, members_text)

            # 6) Slash 명령 응답은 짧게 에페메랄로 안내
            return {
                "type": 4,
                "data": {
                    "content": f"✅ {title} 기준 랜덤 3개를 **채널**로 전송했습니다. (GAS 릴레이)",
                    "flags": 64
                }
            }

        except Exception as e:
            return {"type": 4, "data": {"content": f"⚠️ 실패: {e}", "flags": 64}}

    # 4) 기타 타입은 무시
    return ("", 204)
# --------------------------------
# 로컬 디버그
# --------------------------------
if __name__ == "__main__":
    app.run(debug=True)
